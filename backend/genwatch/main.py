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

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, config as cfgmod
from .api import auth as auth_routes
from .api import control as control_routes
from .api import events as events_routes
from .api import settings as settings_routes
from .api import status as status_routes
from .api import telemetry as telemetry_routes
from .api import ws as ws_routes
from .db import Database
from .modbus.client import MockModbusClient, ModbusClient, SerialModbusClient
from .modbus.poller import Poller
from .modbus.registers import load_register_map
from .services import notify
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
    settings = cfgmod.load(os.environ.get("GENWATCH_CONFIG_PATH"))
    if not settings.auth.jwt_secret:
        # Generate an ephemeral secret so the service still runs without
        # config — tokens won't survive restarts but the service is usable.
        settings = settings.model_copy(
            update={"auth": settings.auth.model_copy(update={"jwt_secret": secrets.token_hex(32)})}
        )
        log.warning("auth.jwt_secret was empty — generated an ephemeral one. Set it in config.yaml for persistence.")

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
        # Production safety: refuse to start so the operator notices the
        # cabling/permission problem instead of silently running a mock
        # that looks live. systemd will retry per its restart policy.
        if not settings.mock:
            msg = (
                f"Modbus serial connect failed on {settings.serial.device}. "
                "Check the USB-to-serial adapter is plugged in, the cable to "
                "the H-100 panel is seated (RS-232 DB9 by default; RS-485 if "
                "the panel is reconfigured), the baud/parity/stop-bits match "
                "the panel, and the 'genwatch' user is in the 'dialout' group. "
                "Run `sudo genwatch doctor` for an itemized check. To run "
                "without hardware (UI demo), set GENWATCH_MOCK=true."
            )
            log.error(msg)
            raise RuntimeError(msg)
        raise RuntimeError("mock client failed to connect — this should never happen")

    bus = EventBus()
    state_machine = StateMachine(regmap, db, bus)
    slack = SlackNotifier(settings.slack, db, site_name=regmap.site.name)
    control_service = ControlService(regmap, client, db, state_machine, slack=slack)

    # Poller callback: persist telemetry, update state machine, push to WS bus.
    async def on_poll(tier, reading, comms):
        try:
            emitted = state_machine.update(reading, comms)
        except Exception as e:  # noqa: BLE001
            log.exception("state machine update failed: %s", e)
            emitted = []

        # Persist a wide row per *base* tier poll (every ~15s by default).
        # Prime polls don't include all metrics — we'd write mostly nulls.
        if tier == "base":
            try:
                db.write_telemetry(
                    ts=reading.ts,
                    values=reading.values,
                    state=state_machine.snap.engine_state,
                    alarm_raw=state_machine.snap.alarm_raw,
                )
            except Exception as e:  # noqa: BLE001
                log.exception("telemetry write failed: %s", e)

        # Always push a snapshot to WS subscribers on the prime cadence.
        if tier == "prime":
            payload = {
                "type": "snapshot",
                "ts": reading.ts,
                "state": state_machine.snap.engine_state,
                "timeInState": state_machine.snap.time_in_state_s,
                "alarmRaw": state_machine.snap.alarm_raw,
                "comms": {
                    "state": comms.state,
                    "successPct": comms.success_pct,
                    "rateMs": comms.rate_ms,
                    "p95LatencyMs": comms.p95_latency_ms,
                },
                "reading": _reading_to_ui(reading.values),
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
    app.state.version = __version__
    app.state.started_at = time.time()

    db.write_event("info", "BOOT", f"GenWatch v{__version__} starting", "mock" if settings.mock else "live")
    await slack.start()
    await poller.start()
    await retention.start()

    # Signal systemd that we're ready, then start a watchdog ping task.
    # If systemd's WatchdogSec is unset (dev / non-systemd), both are no-ops.
    notify.ready()
    watchdog_task: asyncio.Task | None = None
    interval = notify.watchdog_interval_s()
    if interval and interval > 0:
        async def _watchdog_loop() -> None:
            log.info("sd_notify watchdog ticker every %.1fs", interval)
            while True:
                try:
                    await asyncio.sleep(interval)
                except asyncio.CancelledError:
                    return
                # Ping only while the poller is running — if the poller has
                # hung, we stop pinging and systemd will restart us.
                if poller._running:  # type: ignore[attr-defined]
                    notify.watchdog()
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
        await poller.stop()
        await retention.stop()
        await slack.stop()
        await client.close()
        db.write_event("info", "BOOT", "GenWatch stopped", None)


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


def _reading_to_ui(values: dict) -> dict:
    """Translate internal register names to the UI's camelCase keys.

    Centralised here so we don't drift between WS and REST.
    """
    return {
        "rpm": values.get("rpm"),
        "hz": values.get("frequency"),
        "kw": values.get("total_kw"),
        "oilP": values.get("oil_pressure"),
        "coolT": values.get("coolant_temp"),
        "batt": values.get("battery_volts"),
        "vAB": values.get("gen_voltage_ab"),
        "vBC": values.get("gen_voltage_bc"),
        "vCA": values.get("gen_voltage_ca"),
        "iA": values.get("gen_current_a"),
        "iB": values.get("gen_current_b"),
        "iC": values.get("gen_current_c"),
        "fuelPct": values.get("fuel_level_pct"),
        "runHours": values.get("run_hours"),
        "startCount": values.get("start_count"),
    }


def create_app() -> FastAPI:
    setup_logging()
    app = FastAPI(
        title="GenWatch",
        version=__version__,
        description="Generac H-100 monitoring and control over Modbus RTU",
        lifespan=lifespan,
    )

    # CORS — only used in dev when Vite serves on 5173 and API on 8000.
    cors_origins = os.environ.get("GENWATCH_CORS_ORIGINS")
    if cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=[o.strip() for o in cors_origins.split(",")],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    app.include_router(status_routes.router)
    app.include_router(telemetry_routes.router)
    app.include_router(events_routes.router)
    app.include_router(control_routes.router)
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
