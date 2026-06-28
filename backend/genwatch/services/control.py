"""Two-step confirm-token control flow.

Why two-step: a single click that physically affects an industrial
generator is dangerous. The operator must:
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
    issued_at: float          # wall-clock, for display + audit only
    expires_at: float         # wall-clock, returned to the client for its countdown
    expires_monotonic: float  # authoritative expiry — monotonic so an NTP/DST step
    #                           can't extend (or prematurely void) a live token
    verb: str | None = None   # action the token was issued for; None = unbound
    #                           (non-browser clients may omit it). A bound token
    #                           can only be spent on its own action, so a stale
    #                           Start tab can't confirm a Stop with it.


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

# Verbs whose Modbus write the H-100 only honors when the front-panel
# key switch is in AUTO. MANUAL / OFF locally locks out the controller's
# remote-command path, so a write that "succeeds" at the wire level
# would be silently dropped by the panel — leaving the operator looking
# at a UI that says "started" while the engine never cranks. Reject
# server-side instead.
PANEL_AUTO_REQUIRED = {"start", "stop", "exercise", "transfer"}


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

    # Control-relevant data must be at most this many prime-poll cadences old.
    # Tighter than the poller's eviction threshold (TIER_STALE_MULTIPLIER×, ~3),
    # so the command path demands fresher data than mere "not yet evicted".
    _CONTROL_FRESH_MULTIPLIER = 2.0

    def _stale_control_registers(self) -> list[str]:
        """Names of the registers backing panel_mode / engine_state that are
        stale (or never decoded), per the live Reading's per-register ages.

        These are the inputs to the panel-AUTO and state-validity gates; if any
        is stale the gates would be evaluating old data even with healthy comms
        (the prime state block can decode while a key-switch/status single
        persistently fails). Returns [] when everything is fresh.
        """
        reading = getattr(self.state.snap, "last_reading", None)
        ages = getattr(reading, "value_ages", {}) or {}
        cadence_s = max(self.regmap.prime_poll_ms, 1) / 1000.0
        max_age_s = cadence_s * self._CONTROL_FRESH_MULTIPLIER
        now_mono = time.monotonic()

        sources = {r.register for r in self.regmap.engine_state_bits}
        sources |= {r.register for r in self.regmap.panel_mode_bits}

        stale: list[str] = []
        for name in sorted(sources):
            ts = ages.get(name)
            if ts is None or (now_mono - ts) > max_age_s:
                stale.append(name)
        return stale

    async def apply_regmap(self, new_regmap: RegisterMap) -> None:
        """Swap in a freshly-loaded register map (POST /api/registers/reload).

        Acquires _lock so a control write in flight finishes against the
        old map's address before the swap takes effect. Without this, an
        operator-initiated start that races a hot-reload could write to
        an address that no longer exists in the new YAML.
        """
        async with self._lock:
            self.regmap = new_regmap

    async def issue_token(self, operator: str, verb: str | None = None) -> ConfirmToken:
        async with self._lock:
            await self._evict_expired_locked()
            # 128-bit token. The confirm code isn't typed by the operator
            # (the modal fetches and submits it), so there's no UX reason
            # to keep it short — and a 32-bit code guarding a generator
            # start/stop is brute-forceable within the 30 s window.
            tok = secrets.token_hex(16).upper()
            # Avoid collisions
            while tok in self._tokens:
                tok = secrets.token_hex(16).upper()
            now = time.time()
            ct = ConfirmToken(
                token=tok,
                operator=operator,
                issued_at=now,
                expires_at=now + TOKEN_TTL_S,
                expires_monotonic=time.monotonic() + TOKEN_TTL_S,
                verb=verb,
            )
            self._tokens[tok] = ct
            self.db.write_audit(operator, "control.issue_token", f"verb={verb or '*'}", tok, "ok")
            return ct

    async def consume_token(self, token: str, operator: str, verb: str | None = None) -> ConfirmToken:
        """Validate + consume a confirm token (takes self._lock).

        Used by callers that need a confirm-token gate but don't go
        through the full execute() flow — currently the alarm-ack
        endpoint. The control execute() path uses
        _consume_token_locked directly so the entire
        gate-recheck → consume → modbus-write critical section runs
        under a single lock acquisition.

        `verb` is the action being performed; if the token was issued
        bound to a specific action it must match.
        """
        async with self._lock:
            return await self._consume_token_locked(token, operator, verb)

    async def _consume_token_locked(self, token: str, operator: str, verb: str | None = None) -> ConfirmToken:
        """Caller MUST hold self._lock — does not acquire it itself."""
        await self._evict_expired_locked()
        ct = self._tokens.pop(token, None)
        if ct is None:
            self.db.write_audit(operator, "control.consume_token", "missing", token, "denied")
            raise ControlError("token_invalid", "Invalid or expired confirm token", 400)
        if ct.expires_monotonic < time.monotonic():
            self.db.write_audit(operator, "control.consume_token", "expired", token, "denied")
            raise ControlError("token_expired", "Confirm token expired (>30s)", 400)
        if ct.operator != operator:
            self.db.write_audit(operator, "control.consume_token", "operator_mismatch", token, "denied")
            raise ControlError("token_mismatch", "Confirm token was issued to a different operator", 403)
        # Verb binding: a token issued for a specific action can only be
        # spent on that action — stops a stale Start tab from confirming a
        # Stop with its token. Unbound tokens (verb=None, e.g. a non-browser
        # client that didn't specify) remain usable for any action.
        if ct.verb is not None and verb is not None and ct.verb != verb:
            self.db.write_audit(
                operator, "control.consume_token", f"verb_mismatch want={verb} token={ct.verb}", token, "denied"
            )
            raise ControlError(
                "token_action_mismatch", "Confirm token was issued for a different action", 403
            )
        return ct

    async def _evict_expired_locked(self) -> None:
        """Caller MUST hold self._lock — does not acquire it itself."""
        mono = time.monotonic()
        for t, ct in list(self._tokens.items()):
            if ct.expires_monotonic < mono:
                self._tokens.pop(t, None)
                self.db.write_audit(ct.operator, "control.evict_token", "ttl", t, "expired")

    async def execute(self, verb: str, token: str, operator: str, role: str) -> dict:
        if role not in ("operator", "admin"):
            self.db.write_audit(operator, f"control.{verb}", f"role={role}", token, "denied")
            raise ControlError("forbidden", "operator or admin role required", 403)

        if verb not in VERB_TO_CONTROL:
            raise ControlError("unknown_verb", f"unknown control verb {verb!r}", 400)

        ctl_name = VERB_TO_CONTROL[verb]

        # Critical section: regmap-resolve → snap-read → gate-check →
        # token-consume → modbus-write all run under self._lock so
        # concurrent control requests serialize. Without this, two
        # operators (or the same operator's frontend retry) clicking
        # Start in the same window can both observe engine_state=
        # "stopped" pre-lock, both pass to consume_token, both end up
        # writing remote_start to the H-100. Re-reading the state
        # snapshot here (instead of before the lock) ensures the gate
        # is evaluated against the latest poll result and not a
        # potentially-stale value captured before queueing on the lock.
        async with self._lock:
            # Resolve control register against the CURRENT regmap.
            # apply_regmap() also takes self._lock, so a hot-reload
            # that lands while this request was queued is reflected
            # here — we won't write to a register the new YAML doesn't
            # define.
            ctl: ControlDef | None = self.regmap.controls.get(ctl_name)
            if ctl is None:
                self.db.write_audit(operator, f"control.{verb}", "no_register", token, "failed")
                raise ControlError(
                    "no_register",
                    f"control {ctl_name!r} is not present in the register map. "
                    f"Edit registers/h100.yaml or settings.",
                    500,
                )

            # Freshness gate (defense in depth). engine_state is pinned to
            # its last value across a comms outage (services/state.py keeps
            # the last-known state rather than downgrading to 'unknown'),
            # and panel_mode reads from cached register values until they
            # age out — so without this, an operator (or a stale browser
            # tab) could pass the state-validity and panel-AUTO gates below
            # against minutes-old data and fire a start/stop the H-100
            # would silently drop.
            #
            # Two checks (H-3):
            #  1. Comms must be HEALTHY, not just "not lost". A "degraded"
            #     link still serves last-known values and previously slipped
            #     through — but we can't trust the engine/panel state under it.
            #  2. Even when comms reads healthy, the SPECIFIC registers that
            #     back panel_mode and engine_state can be individually stale
            #     (the prime state block can decode while key_switch/status
            #     singles persistently fail; the comms classifier wouldn't
            #     notice). Reject if any of those registers hasn't decoded
            #     within the freshness window.
            comms_state = getattr(self.state.snap.comms, "state", "lost")
            if comms_state != "healthy":
                self.db.write_audit(
                    operator, f"control.{verb}", f"comms={comms_state}", token, "denied"
                )
                raise ControlError(
                    "comms_lost",
                    f"cannot {verb}: H-100 communication is {comms_state.upper()} — "
                    f"live engine state and panel position can't be confirmed. "
                    f"Restore the link (run `genwatch doctor`) before issuing "
                    f"remote commands.",
                    409,
                )
            stale = self._stale_control_registers()
            if stale:
                self.db.write_audit(
                    operator, f"control.{verb}", f"stale={','.join(stale)}", token, "denied"
                )
                raise ControlError(
                    "stale_data",
                    f"cannot {verb}: the H-100 registers that determine engine "
                    f"state / panel position are stale ({', '.join(stale)}) even "
                    f"though the link is up. Wait for a fresh poll or run "
                    f"`genwatch doctor`.",
                    409,
                )

            # Panel key-switch gate. The H-100 ignores remote writes
            # unless the front-panel key is in AUTO; surfacing this
            # server-side turns the failure mode from "silent no-op at
            # the unit" into a visible 409 the UI can render. Snap is
            # re-read here so we catch a key turn that landed between
            # the request arriving and the lock being granted. Verbs
            # not in PANEL_AUTO_REQUIRED skip this gate.
            if verb in PANEL_AUTO_REQUIRED:
                panel_mode = self.state.snap.panel_mode
                if panel_mode != "auto":
                    self.db.write_audit(
                        operator, f"control.{verb}", f"panel_mode={panel_mode}", token, "denied"
                    )
                    raise ControlError(
                        "panel_mode_locked",
                        f"cannot {verb}: H-100 front-panel key switch is {panel_mode.upper()}. "
                        f"Set the panel to AUTO at the unit before issuing remote commands.",
                        409,
                    )

            # Server-side state-validity guard (defense in depth).
            # Re-read under the lock so a state change observed by the
            # poller between request-arrival and lock-grant is honored.
            cur = self.state.snap.engine_state
            allowed = ALLOWED.get(verb, set())
            if cur not in allowed:
                self.db.write_audit(operator, f"control.{verb}", f"state={cur}", token, "denied")
                raise ControlError("invalid_state", f"cannot {verb} while engine is {cur}", 409)

            # All gates passed against the freshest snap. Commit by
            # consuming the token (verb-checked: a token issued for a
            # different action is rejected). If the modbus write below
            # fails, the operator must issue a fresh token to retry —
            # that's intentional (same behavior as before this refactor).
            await self._consume_token_locked(token, operator, verb=verb)

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
        # End critical section. Audit/event/Slack writes don't need to
        # be serialized against other control requests.

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
