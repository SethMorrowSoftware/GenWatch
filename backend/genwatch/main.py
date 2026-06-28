"""FastAPI application factory + lifespan.

Wires the Modbus poller, state machine, retention service and HTTP
routes into a single FastAPI app. Static UI is served from /static.

Lifespan order:
  startup → load config + register map → open DB → connect Modbus client
          → start poller + retention → ready
  shutdown ← stop poller ← stop retention ← close Modbus ← close DB
"""
from __future__ import annotations

import asyncio
import logging
import logging.handlers
import os
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, config as cfgmod
from .api import ats as ats_routes
from .api import auth as auth_routes
from .api import control as control_routes
from .api import events as events_routes
from .api import settings as settings_routes
from .api import status as status_routes
from .api import telemetry as telemetry_routes
from .api import ws as ws_routes
from .db import Database
from .modbus.client import MockModbusClient, ModbusClient, SerialModbusClient, TcpRtuModbusClient
from .modbus.poller import Poller
from .modbus.registers import load_register_map
from .services import notify
from .services.ats import AtsService
from .services.ats_control import AtsControlService
from .services.control import ControlService
from .services.ratelimit import RateLimiter
from .services.retention import RetentionService
from .services.slack import SlackNotifier
from .services.state import EventBus, StateMachine

log = logging.getLogger("genwatch")


def setup_logging() -> None:
    fmt = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    level = os.environ.get("GENWATCH_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(level=level, format=fmt, datefmt="%Y-%m-%dT%H:%M:%S")
    # Quiet pymodbus debug spam unless explicitly asked for it.
    if level != "DEBUG":
        logging.getLogger("pymodbus").setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Capture monotonic service-start moment up front. The watchdog
    # cold-start grace is measured from this point so a slow
    # client.connect() or poller.start() doesn't artificially extend
    # the grace window past what systemd's WatchdogSec expects.
    # Monotonic clock survives NTP steps; wall-clock here would not.
    service_start_mono = time.monotonic()

    settings = cfgmod.load(os.environ.get("GENWATCH_CONFIG_PATH"))

    # ── Secret sanity gates (production) ──────────────────────────────
    # The shipped config template seeds jwt_secret / admin_password_hash
    # with the literal placeholder "REPLACE_ME". The installer rewrites
    # jwt_secret on a *fresh* install, but only WARNS if a re-install,
    # restored backup, or hand-edit leaves the placeholder in place. A
    # truthy-but-known secret previously sailed past the empty-string
    # check below, so the service would boot signing every admin session
    # with a world-known HS256 key — anyone who could reach the port
    # could forge an operator cookie and command the generator. Treat the
    # placeholder, and anything obviously too short to be a 256-bit
    # secret, as "unset".
    PLACEHOLDER = "REPLACE_ME"
    secret = settings.auth.jwt_secret
    secret_unusable = (not secret) or secret == PLACEHOLDER or len(secret) < 32
    if secret_unusable:
        # Mock mode (dev / CI) generates an ephemeral secret to keep the
        # test runner unblocked. Production refuses to start — silently
        # minting an ephemeral secret under `Restart=always` produces a
        # unit that's "up" but logs every operator out on each restart,
        # masking the real problem (config corrupted / secret never set).
        if not settings.mock:
            raise RuntimeError(
                "auth.jwt_secret is unset, the 'REPLACE_ME' placeholder, or "
                "shorter than 32 chars in production mode. Generate one with "
                "`sudo genwatch gensecret` and paste it into "
                "/etc/genwatch/config.yaml, then restart (the installer does "
                "this automatically on a fresh install). Refusing to start: a "
                "missing or guessable secret lets anyone forge an operator "
                "session and command the generator."
            )
        settings = settings.model_copy(
            update={"auth": settings.auth.model_copy(update={"jwt_secret": secrets.token_hex(32)})}
        )
        log.warning("auth.jwt_secret was unset/placeholder — generated an ephemeral one for mock mode (tokens won't survive restart).")

    # admin_password_hash must be a real bcrypt hash in production. With
    # the "REPLACE_ME" placeholder (or any non-bcrypt string) the service
    # would boot "healthy" — green systemd unit, green watchdog — but
    # every /api/auth/login returns 401 because bcrypt.checkpw rejects a
    # non-$2 hash. Fail loudly so an unusable deployment surfaces as an
    # actionable boot error rather than a mysterious lockout. (The
    # installer only starts the service once a real hash is set; this is
    # the runtime backstop for manual starts / restored configs.)
    pw_hash = settings.auth.admin_password_hash
    if not settings.mock and ((not pw_hash) or pw_hash == PLACEHOLDER or not pw_hash.startswith("$2")):
        raise RuntimeError(
            "auth.admin_password_hash is unset or still the 'REPLACE_ME' "
            "placeholder. Generate it with `sudo genwatch hash` and paste the "
            "$2b$... value into /etc/genwatch/config.yaml, then restart. "
            "Refusing to start: login would fail for every operator."
        )

    # Locate register file
    reg_path = Path(settings.modbus.register_file)
    if not reg_path.is_absolute():
        pkg_local = Path(__file__).parent / reg_path
        if pkg_local.exists():
            reg_path = pkg_local
    log.info("Loading register map from %s", reg_path)
    regmap = load_register_map(reg_path)

    db = Database(settings.db_path)
    log.info("Database at %s (%d bytes)", db.path, db.disk_usage_bytes())

    # Choose client implementation
    if settings.mock:
        log.warning("Modbus MOCK mode — no real RS-485 traffic (GENWATCH_MOCK=true)")
        client: ModbusClient = MockModbusClient(regmap)
    elif settings.transport == "tcp":
        client = TcpRtuModbusClient(
            host=settings.modbus_tcp.host,
            port=settings.modbus_tcp.port,
            framer=settings.modbus_tcp.framer,
            timeout_s=settings.modbus_tcp.timeout_s,
            connect_timeout_s=settings.modbus_tcp.connect_timeout_s,
            slave=regmap.slave,
            retries=regmap.retries,
            backoff_s=regmap.backoff_s,
        )
    else:
        client = SerialModbusClient(
            device=settings.serial.device,
            baud=settings.serial.baud,
            parity=settings.serial.parity,
            stopbits=settings.serial.stopbits,
            bytesize=settings.serial.bytesize,
            timeout_s=settings.serial.timeout_s,
            slave=regmap.slave,
            retries=regmap.retries,
            backoff_s=regmap.backoff_s,
        )

    connected = await client.connect()
    if not connected:
        if settings.mock:
            # The mock client should never fail to connect — if it has,
            # something is structurally broken and we want a hard stop.
            raise RuntimeError("mock client failed to connect — this should never happen")
        # Real-hardware connect failure: do NOT exit. systemd would just
        # restart us every few seconds, hammering the journal and making
        # the UI unreachable during the outage. Instead, start in a
        # known-degraded state. The poller will keep trying to reconnect
        # via _ensure_connected on every read; the operator can see
        # "comms lost" in the UI and use it to diagnose. This matches
        # genmon's behaviour: stay up, surface the fault.
        if settings.transport == "tcp":
            target = f"tcp {settings.modbus_tcp.host}:{settings.modbus_tcp.port}"
        else:
            target = f"serial {settings.serial.device}"
        log.error(
            "Modbus connect to %s failed at startup. Service will continue "
            "and keep retrying in the background; the UI will show comms "
            "as LOST until the link comes up. Run `sudo genwatch doctor` "
            "to diagnose cabling/bridge/config.",
            target,
        )

    bus = EventBus()
    slack = SlackNotifier(settings.slack, db, site_name=regmap.site.name)

    # ─── ATS-Pi companion (optional, see docs/integrations/ats-pi-icd.md) ──
    # Constructed BEFORE the StateMachine so the latter can hold a
    # reference to the ATS service for loadSource precedence. The ATS
    # poller is independent — its failure does not affect generator
    # monitoring (and vice versa).
    ats_service: AtsService | None = None
    ats_client: ModbusClient | None = None
    ats_poller: Poller | None = None
    ats_regmap = None
    if settings.ats.enabled:
        ats_reg_path = Path(settings.ats.register_file)
        if not ats_reg_path.is_absolute():
            pkg_local = Path(__file__).parent / ats_reg_path
            if pkg_local.exists():
                ats_reg_path = pkg_local
        log.info("Loading ATS register map from %s", ats_reg_path)
        ats_regmap = load_register_map(ats_reg_path)
        ats_client = TcpRtuModbusClient(
            host=settings.ats.host,
            port=settings.ats.port,
            framer=settings.ats.framer,
            timeout_s=settings.ats.timeout_s,
            connect_timeout_s=settings.ats.connect_timeout_s,
            slave=settings.ats.slave,
            retries=ats_regmap.retries,
            backoff_s=ats_regmap.backoff_s,
        )
        ats_connected = await ats_client.connect()
        if not ats_connected:
            log.error(
                "ATS-Pi connect to %s:%d failed at startup. The ATS poller "
                "will keep retrying in the background; loadSource will fall "
                "back to the H-100-derived value until the link comes up.",
                settings.ats.host, settings.ats.port,
            )
        ats_service = AtsService(
            ats_regmap, db, bus, slack=slack,
            expected_unit_id=settings.ats.expected_unit_id,
        )
        ats_poller = Poller(ats_client, ats_regmap, ats_service.on_poll)
        log.info(
            "ATS-Pi integration enabled — %s:%d slave=%d",
            settings.ats.host, settings.ats.port, settings.ats.slave,
        )
        # Cross-site safety: with no expected_unit_id, the authority gate
        # skips the unit-id check (ats.py::is_authoritative) and GenWatch
        # will trust ANY ATS-Pi answering at this host IP — a wrong-site
        # cross-wire or a re-used IP pointing at the wrong device would go
        # undetected and feed bad position/availability into the operator
        # UI. Warn loudly so the omission is visible in the journal.
        if settings.ats.expected_unit_id is None:
            log.warning(
                "ats.expected_unit_id is NOT set — GenWatch will treat ANY "
                "ATS-Pi reachable at %s:%d as authoritative, with no defence "
                "against a wrong-site cross-wire. Set ats.expected_unit_id in "
                "/etc/genwatch/config.yaml to this site's ATS-Pi unit id "
                "(register 0x0035) to enable the cross-site guard.",
                settings.ats.host, settings.ats.port,
            )
    else:
        log.info("ATS-Pi integration disabled (ats.enabled=false)")

    state_machine = StateMachine(regmap, db, bus, ats_service=ats_service)
    control_service = ControlService(regmap, client, db, state_machine, slack=slack)

    # ATS-Pi command service (Phase 3) — only when the companion is
    # configured. Reuses the H-100 confirm-token store so one token works
    # across both surfaces. Writes go to the independent ATS client.
    ats_control: AtsControlService | None = None
    if ats_service is not None and ats_client is not None and ats_regmap is not None:
        ats_control = AtsControlService(
            ats_regmap, ats_client, db, ats_service, control_service, slack=slack
        )

    # WebSocket snapshot push cadence. Defaults to the prime-poll interval
    # but operators can dial back the UI refresh rate via ws_push_ms
    # (e.g. lower CPU on the Pi, or fewer updates over a slow VPN). We
    # throttle by recording the last-push timestamp; transitions / alarms
    # / comms events always push regardless, so state changes still feel
    # live even with a longer ws_push_ms.
    push_throttle_s = max(0.0, settings.ws_push_ms / 1000.0)
    last_push_ts = [0.0]  # boxed so the nested fn can mutate

    # Poller callback: persist telemetry, update state machine, push to WS bus.
    async def on_poll(tier, reading, comms):
        try:
            emitted = state_machine.update(reading, comms)
        except Exception as e:  # noqa: BLE001
            log.exception("state machine update failed: %s", e)
            emitted = []

        # Persist a wide row per *base* tier poll (every ~15s by default).
        # Prime polls don't include all metrics — we'd write mostly nulls.
        # SQLite WAL writes with synchronous=FULL can spike to 100+ms on
        # a Pi SD card during a checkpoint or fsync. Offloading to a
        # worker thread keeps the event loop responsive to WS pushes,
        # the watchdog ticker, and the next poll cadence — without it,
        # a slow checkpoint can push prime polls past their deadline
        # and chip away at the watchdog grace window.
        if tier == "base":
            try:
                await anyio.to_thread.run_sync(
                    lambda: db.write_telemetry(
                        ts=reading.ts,
                        values=reading.values,
                        state=state_machine.snap.engine_state,
                        alarm_raw=state_machine.snap.alarm_raw,
                    )
                )
            except Exception as e:  # noqa: BLE001
                log.exception("telemetry write failed: %s", e)

        # Throttled snapshot push to WS subscribers (prime cadence with
        # a ws_push_ms floor). Events below still push immediately.
        if tier == "prime" and (reading.ts - last_push_ts[0]) >= push_throttle_s:
            last_push_ts[0] = reading.ts
            # Panel block mirrors GET /api/status — the UI gates the
            # control buttons on panel.mode == 'auto' and shows a chip
            # for MANUAL/OFF. Without this in the push, a key-switch
            # toggle at the unit would not refresh until a manual
            # page reload.
            panel = {
                "mode": state_machine.snap.panel_mode,
                "keySwitchRaw": reading.values.get("key_switch_state"),
                "engineStatusCode": reading.values.get("engine_status_code"),
                "activeAlarmCountHw": reading.values.get("active_alarm_count"),
                "quietTestStatusRaw": reading.values.get("quiettest_status"),
            }
            # ATS-Pi block — only emitted when the companion service is
            # active so non-ATS sites don't pay the payload cost. Mirrors
            # the shape in api/status.py so the frontend can consume it
            # identically whether it arrives via REST seed or WS push.
            ats_push: dict | None = None
            if ats_service is not None:
                a = ats_service.snap
                ats_push = {
                    "enabled": True,
                    "position": a.position,
                    "normalAvailable": a.normal_available,
                    "emergencyAvailable": a.emergency_available,
                    "engineStartCalling": a.engine_start_calling,
                    "atsMode": a.ats_mode,
                    "faultCodes": sorted(a.fault_codes),
                    "lastTransferToGenTs": a.last_transfer_to_gen_ts,
                    "lastRetransferToUtilTs": a.last_retransfer_to_util_ts,
                    "transferCount24h": a.transfer_count_24h,
                    "transferCountLifetime": a.transfer_count_lifetime,
                    "cmdTestActive": a.cmd_test_active,
                    "cmdInhibitActive": a.cmd_inhibit_active,
                    "cmdForceTransferActive": a.cmd_force_transfer_active,
                    "cmdBypassDelayActive": a.cmd_bypass_delay_active,
                    "comms": {
                        "state": a.comms.state,
                        "successPct": a.comms.success_pct,
                    },
                    "authoritative": ats_service.is_authoritative(),
                }

            payload = {
                "type": "snapshot",
                "ts": reading.ts,
                "state": state_machine.snap.engine_state,
                "timeInState": state_machine.snap.time_in_state_s,
                "alarmRaw": state_machine.snap.alarm_raw,
                # Derived utility-vs-generator indicator. Without this,
                # an operator key-switch toggle at the ATS (or an
                # automatic transfer triggered by a utility loss) would
                # not refresh until a manual page reload.
                "loadSource": state_machine.snap.load_source,
                "timeInLoadSource": state_machine.snap.time_in_load_source_s,
                "comms": {
                    "state": comms.state,
                    "successPct": comms.success_pct,
                    "rateMs": comms.rate_ms,
                    "p95LatencyMs": comms.p95_latency_ms,
                },
                "reading": status_routes.reading_to_ui(reading.values),
                "panel": panel,
                "ats": ats_push,
            }
            await bus.publish(payload)

        # Fire transition/alarm events as separate messages, and forward
        # them to Slack (best-effort — failures are logged, not raised).
        for evt in emitted:
            await bus.publish(evt)
            try:
                await _forward_to_slack(slack, evt)
            except Exception as e:  # noqa: BLE001
                log.exception("slack forward failed: %s", e)

    poller = Poller(client, regmap, on_poll)
    retention = RetentionService(db, settings.retention)

    # 5 login attempts then 1 token every 3 minutes (~20/hour steady state).
    login_limiter = RateLimiter(capacity=5, refill_per_s=1.0 / 180.0)
    # ATS command actuation: burst of 3, then 1 every 5 s, per operator.
    # Ample for human operation; stops a buggy/hostile client looping
    # token->command pairs and flapping a maintained relay.
    command_limiter = RateLimiter(capacity=3, refill_per_s=1.0 / 5.0)

    # Attach everything to app.state so route handlers can read it.
    app.state.settings = settings
    app.state.db = db
    app.state.regmap = regmap
    app.state.client = client
    app.state.bus = bus
    app.state.state_machine = state_machine
    app.state.control = control_service
    app.state.poller = poller
    app.state.retention = retention
    app.state.slack = slack
    app.state.login_limiter = login_limiter
    app.state.command_limiter = command_limiter
    app.state.version = __version__
    app.state.started_at = time.time()
    # ATS-Pi companion — None when ats.enabled is false.
    app.state.ats_service = ats_service
    app.state.ats_client = ats_client
    app.state.ats_poller = ats_poller
    app.state.ats_control = ats_control

    if settings.mock:
        boot_mode = "mock"
    elif settings.transport == "tcp":
        boot_mode = f"live · tcp {settings.modbus_tcp.host}:{settings.modbus_tcp.port}"
    else:
        boot_mode = f"live · serial {settings.serial.device}"
    db.write_event("info", "BOOT", f"Castle Generator Monitor v{__version__} starting", boot_mode)
    await slack.start()
    await poller.start()
    if ats_poller is not None:
        await ats_poller.start()
    await retention.start()

    # Signal systemd that we're ready, then start a watchdog ping task.
    # If systemd's WatchdogSec is unset (dev / non-systemd), both are no-ops.
    notify.ready()
    watchdog_task: asyncio.Task | None = None
    interval = notify.watchdog_interval_s()
    if interval and interval > 0:
        # Tick at ~WatchdogSec/4 (half the systemd-recommended interval)
        # rather than WatchdogSec/2. On a busy Pi an SD-card fsync stall
        # can swallow a tick; the tighter cadence leaves several missed
        # ticks of margin before WatchdogSec elapses and SIGKILLs us,
        # avoiding spurious restarts.
        tick = max(1.0, interval / 2.0)
        # We only ping while a *prime* poll has completed within the last
        # `stale_after` seconds. It must be > the prime cadence AND > the
        # check `tick` — otherwise a single poll that lands just after a tick
        # makes the next tick see >stale_after of silence on a perfectly
        # healthy link and spuriously withhold — yet well under systemd's
        # WatchdogSec so a real freeze still triggers a restart. A simple
        # `poller.is_running` flag isn't enough: a deadlocked poll task keeps
        # the flag True while telemetry freezes.
        stale_after = max(tick * 2.0, (regmap.prime_poll_ms / 1000.0) * 6.0)
        # service_start_mono is captured at the top of lifespan so the
        # cold-start grace is measured from the actual service start,
        # not from after poller/client startup completes (which could
        # take many seconds on a slow Modbus connect path and chew
        # into the grace window before the watchdog loop even begins).

        async def _watchdog_loop() -> None:
            log.info(
                "sd_notify watchdog ticker every %.1fs "
                "(stale_after=%.1fs, cold_start_grace=%.0fs)",
                tick, stale_after, notify.WATCHDOG_COLD_START_GRACE_S,
            )
            # Track regime so we only log on transitions instead of
            # spamming the journal every interval while withholding.
            withholding = False
            while True:
                try:
                    await asyncio.sleep(tick)
                except asyncio.CancelledError:
                    return
                if not poller.is_running:
                    continue
                should_ping, reason = notify.should_ping_watchdog(
                    mono_last_prime_good=poller.health.last_prime_good_monotonic,
                    service_start_mono=service_start_mono,
                    now_mono=time.monotonic(),
                    stale_after_s=stale_after,
                )
                if should_ping:
                    notify.watchdog()
                    if withholding:
                        log.info("watchdog: pings resumed (prime poll recovered)")
                        withholding = False
                else:
                    if not withholding:
                        log.warning(
                            "watchdog: withholding ping so systemd can "
                            "restart us — %s", reason,
                        )
                        withholding = True
        watchdog_task = asyncio.create_task(_watchdog_loop(), name="sd-watchdog")

    try:
        yield
    finally:
        log.info("Shutting down...")
        notify.stopping()
        if watchdog_task is not None:
            watchdog_task.cancel()
            try:
                await watchdog_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        # ATS-Pi stops BEFORE the H-100 — independent stack, finish
        # in opposite-of-startup order so any in-flight ATS event
        # publishing completes before the bus tears down.
        if ats_poller is not None:
            await ats_poller.stop()
        if ats_client is not None:
            await ats_client.close()
        await poller.stop()
        await retention.stop()
        await slack.stop()
        await client.close()
        db.write_event("info", "BOOT", "Castle Generator Monitor stopped", None)
        db.close()


async def _forward_to_slack(slack: SlackNotifier, evt: dict) -> None:
    """Dispatch a state machine event to the Slack notifier.

    The notifier itself decides whether to send (config flags + queue).
    This function just maps event-shape to the right call.
    """
    if not slack.is_enabled():
        return
    t = evt.get("type")
    ts = float(evt.get("ts") or time.time())
    if t == "alarm":
        await slack.alert_alarm(
            code=str(evt.get("code", "")),
            desc=str(evt.get("desc", "")),
            severity=str(evt.get("severity", "alarm")),
            ts=ts,
        )
    elif t == "alarm-cleared":
        await slack.alert_alarm_cleared(
            code=str(evt.get("code", "")),
            desc=str(evt.get("desc", "")),
            ts=ts,
        )
    elif t == "transition":
        await slack.alert_state_change(
            old=str(evt.get("from", "")),
            new=str(evt.get("to", "")),
            ts=ts,
        )
    elif t == "comms":
        await slack.alert_comms_change(
            old=str(evt.get("from", "")),
            new=str(evt.get("to", "")),
            success_pct=float(evt.get("successPct", 0.0)),
            ts=ts,
        )
    elif t == "load-source":
        await slack.alert_load_source_change(
            old=str(evt.get("from", "")),
            new=str(evt.get("to", "")),
            ts=ts,
        )


# UI reading transform is owned by api/status.py — see reading_to_ui.


def create_app() -> FastAPI:
    setup_logging()
    app = FastAPI(
        title="Castle Generator Monitor",
        version=__version__,
        description="Generac H-100 monitoring and control over Modbus RTU",
        lifespan=lifespan,
    )

    # CORS — only used in dev when Vite serves on 5173 and API on 8000.
    # Resolves from (in priority order): the GENWATCH_CORS_ORIGINS env
    # var, then the `cors_origins` list in config.yaml. The env var is
    # CSV; the YAML field is a real list. We keep the env-var path so
    # one-off "GENWATCH_CORS_ORIGINS=… genwatch …" still works for ad-hoc
    # debugging.
    cors_env = os.environ.get("GENWATCH_CORS_ORIGINS")
    if cors_env:
        cors_list = [o.strip() for o in cors_env.split(",") if o.strip()]
    else:
        # settings is loaded lazily inside lifespan — peek at the same
        # source (config.yaml + env) so CORS reflects what the operator
        # configured on disk.
        from .config import load as _load_settings
        try:
            _s = _load_settings()
            cors_list = [o.strip() for o in (_s.cors_origins or []) if o.strip()]
        except Exception:  # noqa: BLE001
            cors_list = []
    if cors_list:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_list,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    # CSRF defense-in-depth on state-changing requests. Cookie SameSite
    # is the primary defense, but it's operator-configurable down to
    # 'lax' (and even Strict has known browser quirks for top-level
    # navigations). This middleware adds a hard Origin/Referer check on
    # any non-safe method hitting /api/*, requiring the header to match
    # the request's own host or an entry in cors_origins. Browser-
    # initiated cross-site requests carry the attacker's Origin and are
    # rejected here regardless of cookie policy.
    #
    # Safe methods (GET/HEAD/OPTIONS) are bypassed because they don't
    # mutate state. The /api/auth/login endpoint IS state-changing
    # (sets a cookie) and is gated by this — that's fine because the
    # legitimate browser request carries the same-origin Origin.
    #
    # Non-browser clients (curl, ansible) typically omit Origin and
    # Referer entirely; in that case we accept — same model as the WS
    # Origin check. The threat here is a malicious page running in a
    # browser, not a server-side script with full network credentials.
    SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

    @app.middleware("http")
    async def csrf_origin_check(request: Request, call_next):
        if request.method in SAFE_METHODS:
            return await call_next(request)
        path = request.url.path
        if not path.startswith("/api/"):
            return await call_next(request)
        origin = request.headers.get("origin")
        referer = request.headers.get("referer")
        if not origin and not referer:
            # No Origin/Referer. A genuine same-origin browser fetch always
            # sends Origin on a non-safe method, so this is either a non-browser
            # client or an attempt to strip the header. Require a custom request
            # header (M-8): the GenWatch SPA always sends X-Requested-With, and
            # a cross-site attacker CANNOT set a custom header on a cross-origin
            # request without triggering a CORS preflight that our allowlist
            # rejects. Non-browser clients (curl/ansible) simply add the header.
            if request.headers.get("x-requested-with"):
                return await call_next(request)
            log.warning(
                "csrf: rejecting %s %s — no Origin/Referer and no "
                "X-Requested-With header", request.method, path,
            )
            return JSONResponse(
                {"detail": {"code": "csrf_blocked",
                            "message": "missing Origin/Referer or X-Requested-With"}},
                status_code=403,
            )
        host = request.headers.get("host", "")
        same_origin_ok = {
            f"http://{host}",
            f"https://{host}",
        }
        # Resolve cors_origins from app.state.settings if lifespan has
        # run, otherwise the env/file-loaded copy used to build the
        # CORS middleware above.
        live_cors = getattr(request.app.state, "settings", None)
        if live_cors is not None:
            allowlist = set(same_origin_ok) | set(live_cors.cors_origins or [])
        else:
            allowlist = set(same_origin_ok) | set(cors_list)
        candidate = origin
        if not candidate and referer:
            # Referer is a full URL; strip to scheme+host[:port].
            from urllib.parse import urlparse
            parsed = urlparse(referer)
            if parsed.scheme and parsed.netloc:
                candidate = f"{parsed.scheme}://{parsed.netloc}"
        if candidate not in allowlist:
            log.warning(
                "csrf: rejecting %s %s — origin/referer %r not in allowlist",
                request.method, path, candidate,
            )
            return JSONResponse(
                {"detail": {"code": "csrf_blocked", "message": "request origin not allowed"}},
                status_code=403,
            )
        return await call_next(request)

    app.include_router(status_routes.router)
    app.include_router(telemetry_routes.router)
    app.include_router(events_routes.router)
    app.include_router(control_routes.router)
    app.include_router(ats_routes.router)
    app.include_router(auth_routes.router)
    app.include_router(settings_routes.router)
    app.include_router(ws_routes.router)

    # Static UI — mount only if the built frontend is present. In dev,
    # Vite serves itself on a different port.
    ui_dir_env = os.environ.get("GENWATCH_UI_DIR")
    ui_candidates = [
        Path(ui_dir_env) if ui_dir_env else None,
        Path("/usr/share/genwatch/ui"),
        Path(__file__).parent.parent.parent / "frontend" / "dist",
    ]
    for ui_dir in ui_candidates:
        if ui_dir and ui_dir.exists() and (ui_dir / "index.html").exists():
            log.info("Serving static UI from %s", ui_dir)
            app.mount("/assets", StaticFiles(directory=str(ui_dir / "assets")), name="ui-assets")

            @app.get("/")
            async def root() -> FileResponse:
                return FileResponse(str(ui_dir / "index.html"))

            # SPA fallback for non-API, non-WS routes
            @app.get("/{path:path}", include_in_schema=False)
            async def spa(path: str, request: Request):
                if path.startswith(("api/", "ws/")):
                    return JSONResponse({"detail": "not found"}, status_code=404)
                full = ui_dir / path
                if full.is_file():
                    return FileResponse(str(full))
                return FileResponse(str(ui_dir / "index.html"))

            break
    else:
        log.warning("No built UI found — install the frontend dist into /usr/share/genwatch/ui")

    return app


app = create_app()
