"""Login / logout endpoints."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from ..config import AuthConfig
from ..services.auth import issue_token, verify_password

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginBody(BaseModel):
    password: str


def _client_ip(request: Request) -> str:
    # Behind a reverse proxy this should read X-Forwarded-For. For a bare
    # LAN deployment (the documented GenWatch topology) request.client is
    # authoritative.
    return request.client.host if request.client else "unknown"


def _resolve_cookie_secure(request: Request, auth_cfg: AuthConfig) -> bool:
    """Decide whether to set the ``Secure`` attribute on the session
    cookie for this response.

    - Explicit override in config wins (``cookie_secure: true|false``).
    - Otherwise auto-detect from the request scheme. uvicorn honors the
      ``Forwarded`` / ``X-Forwarded-Proto`` headers from trusted upstream
      hops (default: 127.0.0.1) and updates ``request.url.scheme`` to
      ``https`` accordingly, so a Caddy / Tailscale-serve / nginx
      deployment that terminates TLS on the same host gets Secure
      automatically. Plain-HTTP LAN deployments stay non-Secure.

    The auto-detect path is intentionally lenient: an attacker that can
    spoof X-Forwarded-Proto can at worst trick us into issuing a Secure
    cookie on an HTTP response, which the browser will then refuse to
    send back over HTTP — a self-DoS, not an escalation. The cookie is
    HttpOnly + SameSite=Strict by default so it never leaves a same-site
    request anyway.
    """
    if auth_cfg.cookie_secure is not None:
        return auth_cfg.cookie_secure
    return request.url.scheme == "https"


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
    secure = _resolve_cookie_secure(request, settings.auth)
    response.set_cookie(
        "genwatch_session",
        token,
        max_age=settings.auth.session_hours * 3600,
        httponly=True,
        samesite=settings.auth.cookie_samesite,
        secure=secure,
        path="/",
    )
    request.app.state.db.write_audit(settings.auth.operator_name, "auth.login", "", token[:8] + "...", "ok")
    return {
        "ok": True,
        "operator": settings.auth.operator_name,
        "role": "admin",
    }


@router.post("/logout")
async def logout(request: Request, response: Response) -> dict:
    # delete_cookie must match the Path / SameSite / Secure attributes
    # the cookie was issued with — otherwise some browsers ignore the
    # delete and leave a stale (but useless, since the JWT will expire)
    # cookie in place. Mirror what login() set.
    settings = request.app.state.settings
    response.delete_cookie(
        "genwatch_session",
        path="/",
        samesite=settings.auth.cookie_samesite,
        secure=_resolve_cookie_secure(request, settings.auth),
    )
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
