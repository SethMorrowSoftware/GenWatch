"""Config backup / restore via the UI.

Operators on standby gens rarely back up /etc/genwatch by hand. A
one-click "Download backup" button lets them save the working config +
register map next to their other site documentation; "Restore" rebuilds
the file set after an SD card swap.

Backup format: a gzipped tar with /etc/genwatch/* (config.yaml plus the
shipped register YAML if it's been edited locally). We do NOT include
the SQLite DB — that file is big, has its own backup story (rsync
nightly), and contains telemetry that a typical "restore from
yesterday" workflow would want to keep rather than overwrite.

Admin only. The token + admin_password_hash are sensitive but they're
exactly what you'd want to restore — the operator-side warning is in
the README.
"""
from __future__ import annotations

import io
import tarfile
import time
from pathlib import Path

import yaml
from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse

from .deps import Principal, require_admin

router = APIRouter(prefix="/api/backup", tags=["backup"])


def _safe_extract_members(tf: tarfile.TarFile, target_dir: Path):
    """Yield only safe members (no absolute paths, no .. traversal).

    Tar is a footgun — `extractall` on attacker-controlled input has
    historically let you write anywhere on the filesystem. We filter
    every member here even though the input is admin-uploaded.
    """
    target_resolved = target_dir.resolve()
    for m in tf.getmembers():
        if m.isdev() or m.issym() or m.islnk():
            continue  # no device nodes, no symlinks
        name = m.name
        if name.startswith("/") or ".." in Path(name).parts:
            continue
        dest = (target_dir / name).resolve()
        try:
            dest.relative_to(target_resolved)
        except ValueError:
            continue
        if not m.isfile() and not m.isdir():
            continue
        yield m


@router.get("/download")
async def download_backup(
    request: Request,
    p: Principal = Depends(require_admin),
) -> StreamingResponse:
    """Stream a tar.gz of /etc/genwatch."""
    settings = request.app.state.settings
    cfg_path = Path(settings.config_path) if settings.config_path else None
    if cfg_path is None or not cfg_path.exists():
        raise HTTPException(404, "config file not found on disk")

    etc_dir = cfg_path.parent

    # Resolve the register file: it may be inside /opt/genwatch (package
    # bundle) or /etc/genwatch (operator-overridden). Include only if
    # it sits under etc_dir.
    reg_path: Path | None = None
    try:
        reg_path = settings.register_file_path
        if reg_path is not None and not str(reg_path.resolve()).startswith(str(etc_dir.resolve())):
            reg_path = None
    except Exception:  # noqa: BLE001
        reg_path = None

    request.app.state.db.write_audit(
        p.operator, "backup.download", str(cfg_path), "", "ok"
    )

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        # Add config.yaml at the root of the tar as 'genwatch/config.yaml'.
        tf.add(str(cfg_path), arcname=f"genwatch/{cfg_path.name}")
        if reg_path is not None and reg_path.exists():
            tf.add(str(reg_path), arcname=f"genwatch/registers/{reg_path.name}")
        # Manifest with version + timestamp so the operator can tell two
        # backup tarballs apart at a glance.
        manifest = {
            "schema": 1,
            "created_at": int(time.time()),
            "source_host": request.headers.get("host", "unknown"),
            "version": request.app.state.version,
        }
        manifest_bytes = yaml.safe_dump(manifest, sort_keys=True).encode("utf-8")
        info = tarfile.TarInfo(name="genwatch/MANIFEST.yaml")
        info.size = len(manifest_bytes)
        info.mtime = int(time.time())
        tf.addfile(info, io.BytesIO(manifest_bytes))
    buf.seek(0)

    fname = f"genwatch-backup-{time.strftime('%Y%m%d-%H%M%S')}.tar.gz"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="application/gzip",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.post("/restore")
async def restore_backup(
    request: Request,
    file: UploadFile,
    p: Principal = Depends(require_admin),
) -> dict:
    """Restore /etc/genwatch from an uploaded tar.gz.

    Idempotent across re-uploads. Restart required to pick up
    transport / serial / TCP changes — we surface that in the response
    just like PUT /api/config does. Register-map edits hot-reload via
    /api/registers/reload as usual.
    """
    settings = request.app.state.settings
    cfg_path = Path(settings.config_path) if settings.config_path else None
    if cfg_path is None:
        raise HTTPException(409, "no config path configured")
    etc_dir = cfg_path.parent
    etc_dir.mkdir(parents=True, exist_ok=True)

    body = await file.read()
    if len(body) == 0:
        raise HTTPException(400, "empty file")
    if len(body) > 5 * 1024 * 1024:
        raise HTTPException(413, "backup tarball too large (>5 MB)")

    extracted: list[str] = []
    try:
        with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as tf:
            for m in _safe_extract_members(tf, etc_dir):
                # Strip the leading "genwatch/" prefix that download writes.
                arc_name = m.name
                if arc_name.startswith("genwatch/"):
                    arc_name = arc_name[len("genwatch/"):]
                if not arc_name or arc_name == "MANIFEST.yaml":
                    continue
                target = etc_dir / arc_name
                if m.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                # File: extract to a tmp path then rename for atomicity.
                target.parent.mkdir(parents=True, exist_ok=True)
                src = tf.extractfile(m)
                if src is None:
                    continue
                tmp = target.with_suffix(target.suffix + ".tmp")
                tmp.write_bytes(src.read())
                tmp.replace(target)
                extracted.append(str(target))
    except tarfile.TarError as e:
        raise HTTPException(400, f"bad tarball: {e}")

    request.app.state.db.write_audit(
        p.operator, "backup.restore",
        f"files={len(extracted)}",
        "",
        "ok" if extracted else "failed",
    )

    return {
        "ok": bool(extracted),
        "restored": extracted,
        "restart_required": True,
        "note": "Restart genwatch.service to pick up restored config; "
                "POST /api/registers/reload for register-map edits.",
    }
