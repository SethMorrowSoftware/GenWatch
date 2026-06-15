"""Layered configuration.

Three layers, highest-to-lowest priority:
  1. Environment variables (GENWATCH_*)
  2. /etc/genwatch/config.yaml (deployment)
  3. Built-in defaults

The register map (registers/h100.yaml) is loaded separately and is
hot-reloadable; it is *not* part of this Pydantic model.
"""
from __future__ import annotations

import os
from contextvars import ContextVar
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

DEFAULT_CONFIG_PATHS = [
    "/etc/genwatch/config.yaml",
    "./config.yaml",
]

# Holds the parsed config.yaml for the current load() call so the YAML
# settings source (ranked BELOW env) can read it. A ContextVar keeps it
# reentrancy-safe — load() sets it around construction and resets after.
_yaml_ctx: ContextVar[dict] = ContextVar("genwatch_yaml_cfg", default={})


class _YamlSettingsSource(PydanticBaseSettingsSource):
    """Feeds config.yaml into Settings as a source ranked below env.

    The previous implementation passed the YAML dict as constructor
    kwargs, which in pydantic-settings outranks *every* environment
    source — so any key present in config.yaml silently ignored its
    GENWATCH_* override, the exact opposite of the documented contract.
    Wiring the YAML in as a proper low-priority source restores
    "env wins", and pydantic-settings deep-merges the nested models so an
    env override of one field (e.g. GENWATCH_AUTH__JWT_SECRET) doesn't
    wipe the sibling YAML fields.
    """

    def get_field_value(self, field, field_name):  # noqa: ANN001
        return _yaml_ctx.get().get(field_name), field_name, False

    def prepare_field_value(self, field_name, field, value, value_is_complex):  # noqa: ANN001
        return value

    def __call__(self) -> dict:
        data = _yaml_ctx.get()
        return {name: data[name] for name in self.settings_cls.model_fields if name in data}


class SerialConfig(BaseModel):
    device: str = "/dev/ttyUSB0"
    baud: int = 9600
    parity: Literal["N", "E", "O"] = "N"
    stopbits: Literal[1, 2] = 1
    bytesize: Literal[7, 8] = 8
    timeout_s: float = 1.5


class ModbusTcpConfig(BaseModel):
    """Network bridge to the H-100's serial port.

    Used when ``transport: tcp`` — typically a Lantronix UDS/EDS/xDirect
    or similar terminal server that tunnels raw bytes between a TCP
    socket and a physical RS-232/RS-485 port. The H-100 frames Modbus
    **RTU** on the wire, so the framer must stay 'rtu' even though the
    transport is TCP — this is *not* Modbus/TCP.
    """

    host: str = "192.168.1.249"
    port: int = 10001  # Lantronix raw-TCP default (Channel 1)
    timeout_s: float = 1.5
    connect_timeout_s: float = 3.0
    framer: Literal["rtu", "socket"] = "rtu"


class ModbusConfig(BaseModel):
    slave: int = 100
    read_fc: Literal[3, 4] = 3
    prime_poll_ms: int = 1500
    base_poll_ms: int = 15000
    retries: int = 2
    register_file: str = "registers/h100.yaml"

    @field_validator("slave")
    @classmethod
    def _slave_range(cls, v: int) -> int:
        if not 1 <= v <= 247:
            raise ValueError("Modbus slave must be 1..247")
        return v


class RetentionConfig(BaseModel):
    raw_days: int = 7
    rollup_1m_days: int = 90
    rollup_1h_days: int = 730
    # Info/ok events older than this are pruned. Alarms/warns are never
    # auto-pruned (kept for forensic value).
    events_days: int = 30
    audit_days: int = 0  # 0 = never delete


class AuthConfig(BaseModel):
    # Single-password mode. Hash with passlib bcrypt and paste here.
    # Generate: python -m genwatch.tools hash <password>
    admin_password_hash: str = ""
    operator_name: str = "operator"
    jwt_secret: str = ""  # filled at install-time
    session_hours: int = 12

    # ── Session-cookie hardening ──────────────────────────────────────
    # cookie_secure controls the `Secure` attribute on the JWT cookie:
    #   None  (default) — auto-detect from the request. When the user
    #                     agent reached us over HTTPS (directly, or via
    #                     a reverse proxy that set X-Forwarded-Proto),
    #                     the cookie is issued Secure. On plain-HTTP
    #                     LAN deployments it is not. This is the right
    #                     default for the documented topologies
    #                     (Tailscale, Caddy, plain LAN) — operators
    #                     behind TLS get hardening for free, plain-HTTP
    #                     operators keep working.
    #   True             — always Secure. Use behind a reverse proxy
    #                     that terminates TLS but uvicorn doesn't see
    #                     it as HTTPS (uncommon — only needed if you've
    #                     disabled proxy-header handling).
    #   False            — never Secure. Only for local dev over plain
    #                     HTTP where the auto-detect would also pick
    #                     False; setting it explicitly silences any
    #                     future warning we add.
    cookie_secure: bool | None = None
    # cookie_samesite — defaults to 'strict' for CSRF defense in depth.
    # The two-step confirm-token flow already mitigates classical CSRF
    # against /api/control/*, but strict closes the door on any future
    # endpoint that forgets the token. UX impact: clicking a link from
    # Slack / email into GenWatch will show the login page (cookie not
    # sent on cross-site nav) — one extra login click, no breakage.
    # Use 'lax' only if you have a workflow that relies on cross-site
    # navigation carrying the session. 'none' requires cookie_secure
    # and is rarely useful for this product.
    cookie_samesite: Literal["strict", "lax", "none"] = "strict"

    @field_validator("cookie_samesite")
    @classmethod
    def _samesite_requires_secure(cls, v: str, info) -> str:
        # SameSite=None requires Secure per the browser spec; rejecting
        # this combo at config-load time turns a silent browser-side
        # "cookie discarded" into a startup error operators can fix.
        if v == "none":
            cs = info.data.get("cookie_secure")
            if cs is False:
                raise ValueError(
                    "auth.cookie_samesite='none' requires auth.cookie_secure=true "
                    "(browser refuses to store SameSite=None cookies without Secure)"
                )
        return v


class AtsConfig(BaseModel):
    """ATS-Pi companion device (see docs/integrations/ats-pi-icd.md).

    A second Modbus TCP device on the LAN that physically observes the
    ASCO Series 300 ATS via 18RX module + aux contacts. When healthy,
    its `position` register is the authoritative loadSource for the UI;
    when unreachable, GenWatch falls back to the H-100-derived value.

    Disabled by default — sites without an ATS-Pi see no change. When
    enabled, GenWatch starts a second Modbus client + poller targeting
    the configured host. The two stacks are fully independent: ATS-Pi
    going down does not affect generator monitoring or vice versa.
    """

    enabled: bool = False
    host: str = "192.168.1.250"
    # Default 5020 matches the companion starter's secure-by-default
    # bind (high port → no CAP_NET_BIND_SERVICE / no root on the
    # companion side). Existing deployments with explicit port: 502
    # in their config.yaml are unaffected — only fresh installs see
    # the new default. If your companion is configured for 502, keep
    # `port: 502` in /etc/genwatch/config.yaml.
    port: int = 5020
    # 'socket' = real Modbus/TCP (MBAP framer). The ATS-Pi speaks proper
    # Modbus/TCP, *not* the RTU-over-TCP that the H-100 Lantronix bridge
    # uses. Don't change unless the ATS-Pi side documents otherwise.
    framer: Literal["socket", "rtu"] = "socket"
    slave: int = 1
    timeout_s: float = 1.0
    connect_timeout_s: float = 3.0
    register_file: str = "registers/ats_pi.yaml"
    # When set, GenWatch refuses to mark the ATS poller authoritative
    # unless the device's reported ats_pi_unit_id register matches. Lets
    # the site catch a misconfigured-IP-points-at-wrong-ATS-Pi mistake
    # before bad data influences the operator UI. Left unset (None) the
    # check is skipped — GenWatch then trusts any ATS-Pi at `host`, so the
    # lifespan logs a loud startup warning (see main.py) because a
    # wrong-site cross-wire would otherwise go undetected.
    expected_unit_id: int | None = None


class SlackConfig(BaseModel):
    """Slack alerts via the Web API (chat.postMessage) using a bot token.

    Create a Slack app at https://api.slack.com/apps, add the
    ``chat:write`` scope, install it to your workspace, and invite the
    bot user to the target channel. The token starts with ``xoxb-``.
    """

    enabled: bool = False
    bot_token: str = ""        # xoxb-...
    channel: str = ""          # "#alerts" or channel id "C0123ABCD"
    site_label: str = ""       # overrides site.name in messages

    # Which event types to forward to Slack. All default to True except
    # state-change (chatty — a generator transitions through several
    # states on a normal start) and warning-severity alarms.
    alert_on_alarm: bool = True
    alert_on_warning: bool = True
    alert_on_alarm_cleared: bool = True
    alert_on_state_change: bool = False
    alert_on_command: bool = True
    alert_on_comms_lost: bool = True
    # Utility ↔ generator load-source transitions. Defaults on because
    # an outage / restored-power notification is high-signal — operators
    # want to know immediately, even if engine state change alerts are off.
    alert_on_load_source_change: bool = True


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="GENWATCH_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        # Priority, highest first: explicit init kwargs > env > config.yaml
        # > file secrets/defaults. This is what the module docstring
        # promises; the YAML source is injected here rather than as
        # constructor kwargs so it can't outrank env.
        return (
            init_settings,
            env_settings,
            _YamlSettingsSource(settings_cls),
            file_secret_settings,
        )

    # paths
    data_dir: str = "/var/lib/genwatch"
    config_path: str = ""  # set by load()

    # mock mode: no real serial, synthesised telemetry. Default on if no
    # /dev/ttyUSB0 exists so the service still boots for development.
    mock: bool = False

    # Which Modbus link to use:
    #   "tcp"    — Modbus-RTU over a network serial bridge (Lantronix etc.); uses modbus_tcp.
    #   "serial" — direct USB-to-serial cable on this host; uses serial.
    transport: Literal["serial", "tcp"] = "tcp"

    serial: SerialConfig = Field(default_factory=SerialConfig)
    modbus_tcp: ModbusTcpConfig = Field(default_factory=ModbusTcpConfig)
    modbus: ModbusConfig = Field(default_factory=ModbusConfig)
    retention: RetentionConfig = Field(default_factory=RetentionConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    slack: SlackConfig = Field(default_factory=SlackConfig)
    ats: AtsConfig = Field(default_factory=AtsConfig)

    # WebSocket push cadence — kept at prime poll by default per design
    ws_push_ms: int = 1500

    # CORS — only for development; production serves static UI from same origin
    cors_origins: list[str] = []

    @property
    def db_path(self) -> Path:
        return Path(self.data_dir) / "db.sqlite"

    @property
    def register_file_path(self) -> Path:
        """Absolute path to the register YAML.

        If modbus.register_file is relative, resolve against the
        installed package's registers/ directory first, then the cwd.
        """
        p = Path(self.modbus.register_file)
        if p.is_absolute():
            return p
        pkg_local = Path(__file__).parent / p
        return pkg_local if pkg_local.exists() else p


def _load_yaml(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    with p.open() as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: top-level must be a mapping")
    return data


def load(config_path: str | None = None) -> Settings:
    """Load settings: defaults -> YAML -> env. Env wins."""
    # 1. Find config.yaml
    candidates = [config_path] if config_path else DEFAULT_CONFIG_PATHS
    yaml_data: dict = {}
    chosen = ""
    for c in candidates:
        if not c:
            continue
        if Path(c).exists():
            yaml_data = _load_yaml(c)
            chosen = c
            break

    # 2. Build Settings. The YAML is fed through a dedicated low-priority
    #    source (see _YamlSettingsSource) so environment variables still
    #    win — passing it as kwargs here would invert that.
    token = _yaml_ctx.set(yaml_data)
    try:
        s = Settings(config_path=chosen)
    finally:
        _yaml_ctx.reset(token)

    # 3. Ensure data dir exists. If we can't create it (read-only fs in
    #    test), fall back to a tempdir under the cwd.
    try:
        Path(s.data_dir).mkdir(parents=True, exist_ok=True)
    except (PermissionError, OSError):
        fallback = Path(os.getcwd()) / "var-genwatch"
        fallback.mkdir(parents=True, exist_ok=True)
        s = s.model_copy(update={"data_dir": str(fallback)})

    return s
