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
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_CONFIG_PATHS = [
    "/etc/genwatch/config.yaml",
    "./config.yaml",
]


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


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="GENWATCH_",
        env_nested_delimiter="__",
        extra="ignore",
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

    # 2. Merge into Settings via pydantic
    if yaml_data:
        s = Settings(**yaml_data, config_path=chosen)
    else:
        s = Settings(config_path=chosen)

    # 3. Ensure data dir exists. If we can't create it (read-only fs in
    #    test), fall back to a tempdir under the cwd.
    try:
        Path(s.data_dir).mkdir(parents=True, exist_ok=True)
    except (PermissionError, OSError):
        fallback = Path(os.getcwd()) / "var-genwatch"
        fallback.mkdir(parents=True, exist_ok=True)
        s = s.model_copy(update={"data_dir": str(fallback)})

    return s
