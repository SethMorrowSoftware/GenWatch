"""Two-step confirm-token control flow.

Why two-step: a single click that physically affects a 200 kW generator
is dangerous. The operator must:
  1. POST /api/control/confirm  -> server issues a short-lived token.
  2. POST /api/control/<verb>    with {confirm_token: <token>} within 30s.

Tokens are:
  - opaque random strings (no JWT — these don't need to be portable)
  - single-use (consumed on the first successful POST)
  - tied to the issuing operator
  - audit-logged on issue, use, expiry and denial.

State-validity is enforced server-side too — clicking Start while
running is rejected even if the client missed the disabled-state CSS.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..db import Database
from ..modbus.client import ModbusClient
from ..modbus.registers import ControlDef, RegisterMap
from .state import StateMachine

if TYPE_CHECKING:
    from .slack import SlackNotifier

log = logging.getLogger("genwatch.control")

TOKEN_TTL_S = 30


@dataclass
class ConfirmToken:
    token: str
    operator: str
    issued_at: float
    expires_at: float


VERB_TO_CONTROL = {
    "start": "remote_start",
    "stop": "remote_stop",
    "exercise": "exercise",
    "transfer": "transfer",
}

# Which engine states permit which verbs. Mirrors the design's validity matrix.
ALLOWED = {
    "start":    {"stopped"},
    "stop":     {"running", "exercising", "cranking", "cooling", "alarm"},
    "exercise": {"stopped"},
    "transfer": {"running"},
}


class ControlError(Exception):
    def __init__(self, code: str, message: str, http_status: int = 400):
        self.code = code
        self.http_status = http_status
        super().__init__(message)


class ControlService:
    def __init__(
        self,
        regmap: RegisterMap,
        client: ModbusClient,
        db: Database,
        state: StateMachine,
        slack: "SlackNotifier | None" = None,
    ):
        self.regmap = regmap
        self.client = client
        self.db = db
        self.state = state
        self.slack = slack
        self._tokens: dict[str, ConfirmToken] = {}
        self._lock = asyncio.Lock()

    async def issue_token(self, operator: str) -> ConfirmToken:
        async with self._lock:
            await self._evict_expired()
            tok = secrets.token_hex(4).upper()  # 8 hex chars to match design
            # Avoid collisions
            while tok in self._tokens:
                tok = secrets.token_hex(4).upper()
            now = time.time()
            ct = ConfirmToken(
                token=tok,
                operator=operator,
                issued_at=now,
                expires_at=now + TOKEN_TTL_S,
            )
            self._tokens[tok] = ct
            self.db.write_audit(operator, "control.issue_token", "", tok, "ok")
            return ct

    async def consume_token(self, token: str, operator: str) -> ConfirmToken:
        async with self._lock:
            await self._evict_expired()
            ct = self._tokens.pop(token, None)
            if ct is None:
                self.db.write_audit(operator, "control.consume_token", "missing", token, "denied")
                raise ControlError("token_invalid", "Invalid or expired confirm token", 400)
            if ct.expires_at < time.time():
                self.db.write_audit(operator, "control.consume_token", "expired", token, "denied")
                raise ControlError("token_expired", "Confirm token expired (>30s)", 400)
            if ct.operator != operator:
                self.db.write_audit(operator, "control.consume_token", "operator_mismatch", token, "denied")
                raise ControlError("token_mismatch", "Confirm token was issued to a different operator", 403)
            return ct

    async def _evict_expired(self) -> None:
        now = time.time()
        for t, ct in list(self._tokens.items()):
            if ct.expires_at < now:
                self._tokens.pop(t, None)
                self.db.write_audit(ct.operator, "control.evict_token", "ttl", t, "expired")

    async def execute(self, verb: str, token: str, operator: str, role: str) -> dict:
        if role not in ("operator", "admin"):
            self.db.write_audit(operator, f"control.{verb}", f"role={role}", token, "denied")
            raise ControlError("forbidden", "operator or admin role required", 403)

        if verb not in VERB_TO_CONTROL:
            raise ControlError("unknown_verb", f"unknown control verb {verb!r}", 400)

        ctl_name = VERB_TO_CONTROL[verb]
        ctl: ControlDef | None = self.regmap.controls.get(ctl_name)
        if ctl is None:
            self.db.write_audit(operator, f"control.{verb}", "no_register", token, "failed")
            raise ControlError(
                "no_register",
                f"control {ctl_name!r} is not present in the register map. "
                f"Edit registers/h100.yaml or settings.",
                500,
            )

        # Server-side state-validity guard (defense in depth).
        cur = self.state.snap.engine_state
        allowed = ALLOWED.get(verb, set())
        if cur not in allowed:
            self.db.write_audit(operator, f"control.{verb}", f"state={cur}", token, "denied")
            raise ControlError("invalid_state", f"cannot {verb} while engine is {cur}", 409)

        # Consume token (atomic with state check)
        await self.consume_token(token, operator)

        # Write the Modbus register(s). FC16 multi-register writes use `values`;
        # FC06/FC16 single-register writes use a one-element list.
        write_words = list(ctl.write_values)
        log.warning(
            "CONTROL %s by %s -> %s @0x%04X fc=%d values=%s",
            verb, operator, ctl.name, ctl.addr, ctl.fc,
            [f"0x{w:04X}" for w in write_words],
        )
        if len(write_words) == 1 and ctl.fc == 6:
            res = await self.client.write(ctl.addr, write_words[0], fc=6)
        else:
            res = await self.client.write(ctl.addr, fc=ctl.fc, values=write_words)
        ts = time.time()
        if not res.ok:
            self.db.write_audit(operator, f"control.{verb}", res.error or "modbus_write_failed", token, "failed")
            self.db.write_event(
                severity="warn",
                type_="COMMAND",
                message=f"Operator command {verb} — Modbus write failed",
                meta=res.error or "",
            )
            if self.slack is not None:
                await self.slack.alert_command(verb, operator, "failed", ts)
            raise ControlError("modbus_failed", f"Modbus write failed: {res.error}", 502)

        self.db.write_audit(
            operator,
            f"control.{verb}",
            f"reg={ctl.name}@0x{ctl.addr:04X} fc{ctl.fc} values={[hex(w) for w in write_words]}",
            token,
            "ok",
        )
        self.db.write_event(
            severity="ok",
            type_="COMMAND",
            message=f"Operator command {verb} — confirmed",
            meta=operator,
        )
        if self.slack is not None:
            await self.slack.alert_command(verb, operator, "ok", ts)
        return {
            "ok": True,
            "verb": verb,
            "register": ctl.name,
            "addr": ctl.addr,
            "values": write_words,
        }
