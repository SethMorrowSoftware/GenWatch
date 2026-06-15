"""ATS-Pi command control — ICD v1.0 write side (§6), Phase 3.

Operator-issuable commands to the companion ATS-Pi: Test, Inhibit,
Force-Transfer, Bypass-Delay. Mirrors services/control.py in shape and
reuses its two-step confirm-token store so a single
GET /api/control/confirm token works across both control surfaces and
there is one audited token ledger.

Safety contracts (see docs/integrations/ats-pi-plan.md §5.7 and
docs/integrations/ats-pi-icd.md §6, §8):

  - Authority gate: commands are refused unless the ATS-Pi link is
    *authoritative* (comms healthy + ICD major match + unit-id match).
    A degraded link means we can't trust the read-back, so we don't
    drive outputs. A LOST link returns 502 (matches the UI's "buttons
    disabled, in-flight returns 502" contract); a non-authoritative but
    not-lost link returns 409.
  - Force-Transfer is admin-only, enforced server-side here in addition
    to the route dependency.
  - Force-Transfer is refused while the utility (normal) source is
    available unless the operator passes an explicit ``override`` — you
    don't drop a healthy utility feed onto the generator without
    confirming intent. The UI surfaces this in the confirm-modal copy.
  - The maintained commands (inhibit, force-transfer) ultimately rely on
    the ATS-Pi's ICD §8.3 comms-loss auto-release: if GenWatch dies with
    one asserted, the ATS-Pi releases it within ~30 s on its own. That
    backstop lives on the companion side and must be bench-verified
    before go-live (plan §8 commissioning checklist).

The actual ASCO contact debounce / pulse timing is entirely the
ATS-Pi's responsibility (ICD §2). GenWatch only writes the command
register and reads back the asserted state.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..db import Database
from ..modbus.client import ModbusClient
from ..modbus.registers import ControlDef, RegisterMap
from .control import ControlError, ControlService

if TYPE_CHECKING:
    from .ats import AtsService
    from .slack import SlackNotifier

log = logging.getLogger("genwatch.ats_control")


_ROLE_RANK = {"viewer": 0, "operator": 1, "admin": 2}


def _role_satisfies(role: str, required: str) -> bool:
    return _ROLE_RANK.get(role, 0) >= _ROLE_RANK.get(required, 99)


# Faults that block ASSERTING a command. A release (de-energizing a relay)
# is the fail-safe direction and is never blocked by a fault. INPUT_FAULT /
# CALIBRATION already drop authority in AtsService.is_authoritative() (so an
# assert is refused as "not authoritative" before reaching this set); they
# are listed here too so the gate is correct even if authority semantics
# change. OUTPUT_FAULT = a driven relay disagrees with its read-back (a stuck
# relay — don't pile another command on top); MODE_UNKNOWN = the ATS mode
# can't be verified, so we can't confirm the command is permitted in the
# current mode (ICD §6.1).
_ASSERT_BLOCKING_FAULTS = frozenset({
    "ATS_PI_INPUT_FAULT",
    "ATS_PI_CALIBRATION",
    "ATS_PI_OUTPUT_FAULT",
    "ATS_PI_MODE_UNKNOWN",
})


@dataclass(frozen=True)
class AtsCommandSpec:
    """Maps an API command to ATS-Pi control register(s) + policy."""

    kind: str  # "momentary" | "maintained"
    assert_control: str  # control name written to assert / pulse
    release_control: str | None  # control name written to release (maintained only)
    role: str  # minimum role required
    desc: str


# command -> spec. Control names resolve against registers/ats_pi.yaml.
ATS_COMMAND_SPECS: dict[str, AtsCommandSpec] = {
    "test": AtsCommandSpec(
        "momentary", "ats_test", None, "operator", "momentary test transfer"
    ),
    "inhibit": AtsCommandSpec(
        "maintained", "ats_inhibit_assert", "ats_inhibit_release", "operator", "inhibit transfer"
    ),
    "force_transfer": AtsCommandSpec(
        "maintained", "ats_force_assert", "ats_force_release", "admin", "force transfer to generator"
    ),
    "bypass_delay": AtsCommandSpec(
        "momentary", "ats_bypass_delay", None, "operator", "bypass transfer time delay"
    ),
}


class AtsControlService:
    def __init__(
        self,
        regmap: RegisterMap,
        client: ModbusClient,
        db: Database,
        ats_service: "AtsService",
        control_service: ControlService,
        slack: "SlackNotifier | None" = None,
    ):
        self.regmap = regmap
        self.client = client
        self.db = db
        self.ats = ats_service
        # Reuse the H-100 control service's confirm-token store so a
        # single confirm token works for both surfaces and the audit
        # ledger is unified.
        self.control = control_service
        self.slack = slack
        self._lock = asyncio.Lock()

    async def execute(
        self,
        command: str,
        *,
        token: str,
        operator: str,
        role: str,
        assert_: bool = True,
        override: bool = False,
    ) -> dict:
        spec = ATS_COMMAND_SPECS.get(command)
        if spec is None:
            raise ControlError("unknown_command", f"unknown ATS command {command!r}", 400)

        # Role gate (defense in depth — routes also gate via Depends).
        if not _role_satisfies(role, spec.role):
            self.db.write_audit(operator, f"ats.{command}", f"role={role}", token, "denied")
            raise ControlError(
                "forbidden", f"{spec.role} role required for ATS {command}", 403
            )

        # Critical section: authority/override gate → token-consume →
        # write, all under the ATS control lock so concurrent ATS
        # commands serialize. consume_token acquires the H-100 control
        # lock internally and releases it before we write; control.execute
        # never acquires this lock, so there's no lock-ordering cycle.
        async with self._lock:
            # A LOST link can't carry a write in either direction.
            comms_state = self.ats.snap.comms.state
            if comms_state == "lost":
                self.db.write_audit(operator, f"ats.{command}", "ats_comms=lost", token, "denied")
                raise ControlError(
                    "ats_comms_lost",
                    f"cannot {command}: ATS-Pi link is LOST. Commands are "
                    f"disabled until the companion device is reachable.",
                    502,
                )

            # Resolve the control register. Maintained commands pick
            # assert vs release; momentary commands always assert/pulse.
            if spec.kind == "maintained":
                ctl_name = spec.assert_control if assert_ else spec.release_control
            else:
                ctl_name = spec.assert_control

            # An assert drives a relay closed (or pulses one); a release
            # drives it open. Releasing is the fail-safe direction, so the
            # authority and fault gates below apply to ASSERTS ONLY — an
            # operator (or the UI) must always be able to back a command out,
            # even when the link is degraded or the ATS-Pi is faulted.
            is_assert = spec.kind != "maintained" or assert_
            if is_assert:
                # Asserts require a trustworthy, authoritative link: we must
                # be able to believe the read-back and the reported position
                # before driving an output. A degraded link, ICD-version or
                # unit-id mismatch, or a position-tainting fault (INPUT_FAULT /
                # CALIBRATION — including the hybrid "reachable but blind"
                # serial-sense loss) all drop authority.
                if not self.ats.is_authoritative():
                    self.db.write_audit(operator, f"ats.{command}", "not_authoritative", token, "denied")
                    raise ControlError(
                        "ats_not_authoritative",
                        f"cannot {command}: ATS-Pi link is not authoritative "
                        f"(comms degraded, ICD-version/unit-id mismatch, or the "
                        f"ATS-Pi cannot currently trust its own position sense). "
                        f"Command refused for safety.",
                        409,
                    )
                # Additionally refuse an assert on a fault that authority does
                # not already cover (OUTPUT_FAULT / MODE_UNKNOWN). Releases are
                # unaffected — handled by the is_assert guard above.
                blocking = self.ats.snap.fault_codes & _ASSERT_BLOCKING_FAULTS
                if blocking:
                    codes = ",".join(sorted(blocking))
                    self.db.write_audit(operator, f"ats.{command}", f"fault:{codes}", token, "denied")
                    raise ControlError(
                        "ats_fault",
                        f"cannot {command}: ATS-Pi reports {codes}. Asserting a "
                        f"command is refused until the fault clears; a release is "
                        f"still permitted.",
                        409,
                    )

                # Force-transfer healthy-utility guard. Don't drop a healthy
                # utility feed onto the generator without an explicit override.
                if command == "force_transfer" and not override:
                    if self.ats.snap.normal_available is True:
                        self.db.write_audit(
                            operator, f"ats.{command}", "normal_available_no_override", token, "denied"
                        )
                        raise ControlError(
                            "override_required",
                            "force-transfer refused: the utility (normal) source is "
                            "AVAILABLE. Re-issue with override to transfer the load "
                            "onto the generator anyway.",
                            409,
                        )

            ctl: ControlDef | None = self.regmap.controls.get(ctl_name) if ctl_name else None
            if ctl is None:
                self.db.write_audit(operator, f"ats.{command}", f"no_register:{ctl_name}", token, "failed")
                raise ControlError(
                    "no_register",
                    f"ATS control {ctl_name!r} is not present in the ATS register map.",
                    500,
                )

            # Commit by consuming the token (reuses H-100 token store,
            # audited there). Verb-bound to this command so a token issued
            # for one ATS action can't be spent on another. A failed write
            # below requires a fresh token to retry — same discipline as
            # the H-100 control path.
            await self.control.consume_token(token, operator, verb=command)

            write_words = list(ctl.write_values)
            log.warning(
                "ATS CONTROL %s by %s -> %s @0x%04X fc=%d values=%s assert=%s override=%s",
                command, operator, ctl.name, ctl.addr, ctl.fc,
                [f"0x{w:04X}" for w in write_words], assert_, override,
            )
            if len(write_words) == 1 and ctl.fc == 6:
                res = await self.client.write(ctl.addr, write_words[0], fc=6)
            else:
                res = await self.client.write(ctl.addr, fc=ctl.fc, values=write_words)
            ts = time.time()
        # End critical section.

        if not res.ok:
            self.db.write_audit(operator, f"ats.{command}", res.error or "modbus_write_failed", token, "failed")
            self.db.write_event(
                severity="warn",
                type_="ATS_COMMAND",
                message=f"ATS command {command} — Modbus write failed",
                meta=res.error or "",
            )
            raise ControlError("ats_modbus_failed", f"ATS Modbus write failed: {res.error}", 502)

        # Record the write so AtsService's read-back edge detector can
        # tell this command's echo apart from an externally-initiated
        # change (§8.3 auto-release, companion restart, foreign client).
        self.ats.note_command_write(command, assert_)

        self.db.write_audit(
            operator,
            f"ats.{command}",
            f"reg={ctl.name}@0x{ctl.addr:04X} fc{ctl.fc} "
            f"values={[hex(w) for w in write_words]} assert={assert_} override={override}",
            token,
            "ok",
        )
        verb = "asserted" if (spec.kind == "maintained" and assert_) else (
            "released" if spec.kind == "maintained" else "pulsed"
        )
        self.db.write_event(
            severity="ok",
            type_="ATS_COMMAND",
            message=f"ATS command {command} {verb} — confirmed",
            meta=operator,
        )
        _ = ts  # reserved for future Slack command notification
        return {
            "ok": True,
            "command": command,
            "register": ctl.name,
            "addr": ctl.addr,
            "values": write_words,
            "assert": assert_,
            "override": override,
        }
