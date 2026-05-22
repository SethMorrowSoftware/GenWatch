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
        "wsPushMs": s.ws_push_ms,
    }


class ConfigUpdate(BaseModel):
    serial: dict | None = None
    modbus: dict | None = None
    retention: dict | None = None
    ws_push_ms: int | None = None


@router.put("/config")
async def update_config(
    request: Request,
    body: ConfigUpdate,
    p: Principal = Depends(require_admin),
) -> dict:
    s = request.app.state.settings
    if not s.config_path:
        raise HTTPException(409, "no config.yaml path configured — set GENWATCH_CONFIG_PATH or copy deploy/genwatch.yaml.example")

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

    # Atomic write: tmp -> rename
    tmp = cfg_path.with_suffix(cfg_path.suffix + ".tmp")
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    with tmp.open("w") as f:
        yaml.safe_dump(on_disk, f, default_flow_style=False, sort_keys=False)
    shutil.move(tmp, cfg_path)

    request.app.state.db.write_audit(p.operator, "config.update", str(body.model_dump()), "", "ok")
    log.info("config updated on disk by %s — restart required for serial/modbus changes", p.operator)
    return {"ok": True, "configPath": str(cfg_path), "restart_required": True}


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
