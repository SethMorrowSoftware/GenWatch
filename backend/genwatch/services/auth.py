"""Single-password auth with JWT session tokens.

Design choice (per user input): one bcrypt-hashed admin password in
config, JWT cookie session, single operator identity. No DB-backed
users table for MVP. Trade-off: simpler ops, fits a single-site Pi
behind Tailscale; doesn't scale to multi-user audit logs (but the
audit log still records *the request*, just under one operator name).

Token validity: configurable session_hours (default 12). On the wire we
use HS256 — secret is generated at install time and stored in config.
"""
from __future__ import annotations

import logging
import time
from typing import Literal

import bcrypt
import jwt

log = logging.getLogger("genwatch.auth")

Role = Literal["viewer", "operator", "admin"]

# bcrypt's 72-byte secret limit — silently truncate to match the standard
# Python bcrypt binding behavior. Operators rarely pick passwords this
# long, but if they do, paste-from-keepass shouldn't crash login.
_BCRYPT_MAX = 72


class AuthError(Exception):
    pass


def hash_password(plain: str) -> str:
    raw = plain.encode("utf-8")[:_BCRYPT_MAX]
    return bcrypt.hashpw(raw, bcrypt.gensalt(rounds=12)).decode("ascii")


def verify_password(plain: str, hashed: str) -> bool:
    if not hashed or not plain:
        return False
    try:
        return bcrypt.checkpw(plain.encode("utf-8")[:_BCRYPT_MAX], hashed.encode("ascii"))
    except Exception:  # noqa: BLE001
        return False


def issue_token(*, secret: str, operator: str, role: Role = "admin", hours: int = 12) -> str:
    if not secret:
        raise AuthError("auth not configured (missing jwt_secret)")
    now = int(time.time())
    payload = {
        "sub": operator,
        "role": role,
        "iat": now,
        "exp": now + hours * 3600,
    }
    return jwt.encode(payload, secret, algorithm="HS256")


def decode_token(*, secret: str, token: str) -> dict:
    if not secret:
        raise AuthError("auth not configured")
    try:
        return jwt.decode(token, secret, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        raise AuthError("token expired")
    except jwt.InvalidTokenError as e:
        raise AuthError(f"invalid token: {e}")
