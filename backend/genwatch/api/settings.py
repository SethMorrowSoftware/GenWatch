"""Read/write the deployment config + register map.

GET  /api/config        full effective config (sanitized — no secrets)
PUT  /api/config        admin-only; writes to disk, reloads poller
GET  /api/registers     current register map (for the Settings UI table)
POST /api/registers/reload  re-read registers.yaml from disk
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

import yaml
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from .deps import Principal, require_admin

log = logging.getLogger("genwatch.api.settings")

router = APIRouter(prefix="/api", tags=["settings"])


@router.get("/config")
async def get_config(request: Request) -> dict:
    s = request.app.state.settings
    return {
        "configPath": s.config_path,
        "mock": s.mock,
        "serial": s.serial.model_dump(),
        "modbus": s.modbus.model_dump(),
        "retention": s.retention.model_dump(),
        "auth": {
            "operatorName": s.auth.operator_name,
            "sessionHours": s.auth.session_hours,
            "passwordConfigured": bool(s.auth.admin_password_hash),
            "jwtSecretConfigured": bool(s.auth.jwt_secret),
        },
        "slack": {
            "enabled": s.slack.enabled,
            "channel": s.slack.channel,
            "siteLabel": s.slack.site_label,
            # Never return the bot token itself; just confirm presence.
            "botTokenConfigured": bool(s.slack.bot_token),
            "alertOnAlarm": s.slack.alert_on_alarm,
            "alertOnWarning": s.slack.alert_on_warning,
            "alertOnAlarmCleared": s.slack.alert_on_alarm_cleared,
            "alertOnStateChange": s.slack.alert_on_state_change,
            "alertOnCommand": s.slack.alert_on_command,
            "alertOnCommsLost": s.slack.alert_on_comms_lost,
        },
        "wsPushMs": s.ws_push_ms,
    }


class SlackUpdate(BaseModel):
    enabled: bool | None = None
    # bot_token: empty string clears, None preserves on-disk value.
    bot_token: str | None = None
    channel: str | None = None
    site_label: str | None = None
    alert_on_alarm: bool | None = None
    alert_on_warning: bool | None = None
    alert_on_alarm_cleared: bool | None = None
    alert_on_state_change: bool | None = None
    alert_on_command: bool | None = None
    alert_on_comms_lost: bool | None = None


class ConfigUpdate(BaseModel):
    serial: dict | None = None
    modbus: dict | None = None
    retention: dict | None = None
    slack: SlackUpdate | None = None
    ws_push_ms: int | None = None


# Fields in this Slack update that, if present in the request, require
# writing the live in-memory SlackConfig + restarting the notifier's
# config snapshot. Used by the PUT handler below.
_SLACK_HOTRELOAD_FIELDS = {
    "enabled",
    "bot_token",
    "channel",
    "site_label",
    "alert_on_alarm",
    "alert_on_warning",
    "alert_on_alarm_cleared",
    "alert_on_state_change",
    "alert_on_command",
    "alert_on_comms_lost",
}


@router.put("/config")
async def update_config(
    request: Request,
    body: ConfigUpdate,
    p: Principal = Depends(require_admin),
) -> dict:
    s = request.app.state.settings
    if not s.config_path:
        raise HTTPException(
            409,
            "no config.yaml path configured — set GENWATCH_CONFIG_PATH or copy deploy/config.yaml.example",
        )

    cfg_path = Path(s.config_path)
    # Read existing on-disk yaml (preserve fields we don't touch)
    on_disk: dict = {}
    if cfg_path.exists():
        with cfg_path.open() as f:
            on_disk = yaml.safe_load(f) or {}

    if body.serial:
        on_disk.setdefault("serial", {}).update(body.serial)
    if body.modbus:
        on_disk.setdefault("modbus", {}).update(body.modbus)
    if body.retention:
        on_disk.setdefault("retention", {}).update(body.retention)
    if body.ws_push_ms is not None:
        on_disk["ws_push_ms"] = int(body.ws_push_ms)

    slack_changed = False
    if body.slack is not None:
        # Pull only the fields the operator actually sent (exclude None
        # → don't touch). bot_token == "" is explicit clear.
        slack_patch = body.slack.model_dump(exclude_none=True)
        if slack_patch:
            on_disk.setdefault("slack", {}).update(slack_patch)
            slack_changed = True

    # Atomic write: tmp -> rename
    tmp = cfg_path.with_suffix(cfg_path.suffix + ".tmp")
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    with tmp.open("w") as f:
        yaml.safe_dump(on_disk, f, default_flow_style=False, sort_keys=False)
    shutil.move(tmp, cfg_path)

    # Slack settings hot-reload — no restart required.
    if slack_changed:
        from ..config import SlackConfig
        # Sanitize what we audit (don't echo the token back to the audit log)
        slack_for_audit = {**slack_patch}
        if "bot_token" in slack_for_audit:
            slack_for_audit["bot_token"] = "<set>" if slack_for_audit["bot_token"] else "<cleared>"

        merged = {**s.slack.model_dump(), **slack_patch}
        new_slack_cfg = SlackConfig(**merged)
        new_settings = s.model_copy(update={"slack": new_slack_cfg})
        request.app.state.settings = new_settings
        notifier = getattr(request.app.state, "slack", None)
        if notifier is not None:
            notifier.update_config(new_slack_cfg)
    else:
        slack_for_audit = None

    audit_detail = body.model_dump(exclude_none=True)
    if "slack" in audit_detail and slack_for_audit is not None:
        audit_detail["slack"] = slack_for_audit
    request.app.state.db.write_audit(p.operator, "config.update", str(audit_detail), "", "ok")

    # Slack-only changes don't require a restart; serial/modbus do.
    restart_required = any(v is not None for v in (body.serial, body.modbus, body.retention, body.ws_push_ms))
    log.info(
        "config updated on disk by %s (slack=%s, restart_required=%s)",
        p.operator, slack_changed, restart_required,
    )
    return {
        "ok": True,
        "configPath": str(cfg_path),
        "restart_required": restart_required,
        "slack_updated": slack_changed,
    }


@router.post("/slack/test")
async def test_slack(
    request: Request,
    p: Principal = Depends(require_admin),
) -> dict:
    """Send a synchronous test message to Slack.

    Uses the current in-memory configuration (which reflects the most
    recent PUT /api/config). Returns 200 with ``{ok, detail}`` even on
    failure so the UI can surface the Slack error verbatim instead of
    swallowing it as an HTTP error.
    """
    notifier = getattr(request.app.state, "slack", None)
    if notifier is None:
        raise HTTPException(503, "slack notifier not initialised")
    ok, detail = await notifier.test()
    request.app.state.db.write_audit(
        p.operator, "slack.test", detail if not ok else "", "", "ok" if ok else "failed"
    )
    return {"ok": ok, "detail": detail}


@router.get("/registers")
async def get_registers(request: Request) -> dict:
    rm = request.app.state.regmap
    snap = request.app.state.state_machine.snap
    reading = snap.last_reading.values

    out = []
    for r in rm.registers:
        out.append({
            "addr": f"0x{r.addr:04X}",
            "name": r.name,
            "fc": f"0{r.fc}",
            "type": r.type,
            "tier": r.tier,
            "group": r.group,
            "unit": r.unit,
            "scale": r.scale if r.scale != 1.0 else None,
            "value": reading.get(r.name),
        })
    for c in rm.controls.values():
        out.append({
            "addr": f"0x{c.addr:04X}",
            "name": c.name,
            "fc": f"0{c.fc}",
            "type": "u16",
            "tier": "controls",
            "group": "Controls · write-gated",
            "unit": "cmd",
            "scale": None,
            "value": None,
        })

    return {
        "path": str(rm.path),
        "slave": rm.slave,
        "primePollMs": rm.prime_poll_ms,
        "basePollMs": rm.base_poll_ms,
        "registers": out,
    }


@router.post("/registers/reload")
async def reload_registers(
    request: Request,
    p: Principal = Depends(require_admin),
) -> dict:
    from ..modbus.registers import load_register_map

    rm_old = request.app.state.regmap
    try:
        rm_new = load_register_map(rm_old.path)
    except Exception as e:  # noqa: BLE001
        request.app.state.db.write_audit(p.operator, "registers.reload", str(e), "", "failed")
        raise HTTPException(400, f"register map invalid: {e}")

    # Hot-swap into app state and notify dependents that point at the same
    # object. Poller doesn't hot-swap mid-run — it captures the reference
    # at start. For a clean rebind, the caller should also POST /api/restart.
    request.app.state.regmap = rm_new
    request.app.state.db.write_audit(p.operator, "registers.reload", str(rm_new.path), "", "ok")
    request.app.state.db.write_event(
        severity="info",
        type_="CONFIG",
        message=f"Register file reloaded — {rm_new.path.name}",
        meta=f"{len(rm_new.registers)} regs · {len(rm_new.controls)} controls",
    )
    return {"ok": True, "registers": len(rm_new.registers), "controls": len(rm_new.controls)}
