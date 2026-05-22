"""Shared dependencies: app state, auth, role gates."""
from __future__ import annotations

from typing import Literal

from fastapi import Depends, HTTPException, Request, status

from ..services.auth import AuthError, decode_token


class Principal:
    def __init__(self, *, operator: str, role: Literal["viewer", "operator", "admin"]):
        self.operator = operator
        self.role = role


def get_app_state(request: Request):
    return request.app.state


def get_principal(request: Request) -> Principal:
    """Read auth from cookie *or* Authorization header.

    Cookie is the normal path for the browser UI; header is for CLI use.
    """
    token = request.cookies.get("genwatch_session")
    if not token:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth.split(" ", 1)[1]
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not authenticated")

    settings = request.app.state.settings
    try:
        payload = decode_token(secret=settings.auth.jwt_secret, token=token)
    except AuthError as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(e))

    return Principal(
        operator=payload.get("sub", "operator"),
        role=payload.get("role", "viewer"),
    )


def require_operator(p: Principal = Depends(get_principal)) -> Principal:
    if p.role not in ("operator", "admin"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "operator role required")
    return p


def require_admin(p: Principal = Depends(get_principal)) -> Principal:
    if p.role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "admin role required")
    return p
