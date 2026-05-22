"""Login / logout endpoints."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from ..services.auth import issue_token, verify_password

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginBody(BaseModel):
    password: str


def _client_ip(request: Request) -> str:
    # Behind a reverse proxy this should read X-Forwarded-For. For a bare
    # LAN deployment (the documented GenWatch topology) request.client is
    # authoritative.
    return request.client.host if request.client else "unknown"


@router.post("/login")
async def login(request: Request, body: LoginBody, response: Response) -> dict:
    settings = request.app.state.settings
    if not settings.auth.admin_password_hash:
        raise HTTPException(503, "auth not initialized — set admin_password_hash in config")

    ip = _client_ip(request)
    limiter = getattr(request.app.state, "login_limiter", None)
    if limiter is not None and not limiter.check(ip):
        retry = limiter.retry_after_s(ip)
        request.app.state.db.write_audit("anonymous", "auth.login", f"ip={ip}", "", "rate_limited")
        raise HTTPException(
            429,
            detail={"code": "rate_limited", "message": f"too many attempts; retry in {retry}s"},
            headers={"Retry-After": str(retry)},
        )

    if not verify_password(body.password, settings.auth.admin_password_hash):
        # Audit failed logins (no operator known yet, attribute to "anonymous")
        request.app.state.db.write_audit("anonymous", "auth.login", f"ip={ip}", "", "denied")
        raise HTTPException(401, "invalid password")

    if limiter is not None:
        limiter.reset(ip)

    token = issue_token(
        secret=settings.auth.jwt_secret,
        operator=settings.auth.operator_name,
        role="admin",
        hours=settings.auth.session_hours,
    )
    response.set_cookie(
        "genwatch_session",
        token,
        max_age=settings.auth.session_hours * 3600,
        httponly=True,
        samesite="lax",
        secure=False,  # local-network deployment; TLS-terminated by Tailscale/reverse proxy
        path="/",
    )
    request.app.state.db.write_audit(settings.auth.operator_name, "auth.login", "", token[:8] + "...", "ok")
    return {
        "ok": True,
        "operator": settings.auth.operator_name,
        "role": "admin",
    }


@router.post("/logout")
async def logout(response: Response) -> dict:
    response.delete_cookie("genwatch_session", path="/")
    return {"ok": True}


@router.get("/me")
async def me(request: Request) -> dict:
    # Light-weight identity check used by the UI shell. Returns 200 even
    # when unauthenticated so the UI can redirect to login.
    token = request.cookies.get("genwatch_session")
    if not token:
        return {"authenticated": False}
    from ..services.auth import AuthError, decode_token
    try:
        payload = decode_token(secret=request.app.state.settings.auth.jwt_secret, token=token)
        return {"authenticated": True, "operator": payload.get("sub"), "role": payload.get("role")}
    except AuthError:
        return {"authenticated": False}
