"""ATS-Pi companion device integration.

Consumes the ICD v1.0 read side from a companion ATS-Pi device polled
over Modbus TCP. The ATS-Pi directly observes the ASCO Series 300's
auxiliary contacts and 18RX module, so its `position` reading is
ground truth — more accurate than GenWatch's H-100-derived loadSource.

When ATS-Pi comms are healthy, this service is the authoritative source
of "what source is supplying the load." When comms degrade or the
device is absent, GenWatch's StateMachine falls back to the existing
H-100-electrical derivation in services/state.py — no behaviour change
from the user's perspective other than a "(via gen telemetry)"
provenance subscript in the UI.

Architecture parallels services/state.py:StateMachine — owns a
snapshot, derives transition events from polled state changes,
forwards to the event bus, persists to the DB, and (optionally)
forwards to Slack. The two services are wholly independent: failure
of one cannot affect the other.

See:
  - docs/integrations/ats-pi-icd.md      (wire contract)
  - docs/integrations/ats-pi-plan.md     (phased integration plan)
  - backend/genwatch/registers/ats_pi.yaml  (register layout)
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..db import Database
from ..modbus.poller import CommsHealth, Reading
from ..modbus.registers import RegisterMap

if TYPE_CHECKING:
    from .slack import SlackNotifier
    from .state import EventBus

log = logging.getLogger("genwatch.ats")


# This consumer is built against ICD v1.x. A major version bump on the
# ATS-Pi side requires updating this constant and the consumer logic.
EXPECTED_ICD_MAJOR = 1

# Decoding tables for the enum registers. Values outside the documented
# range decode to 'unknown' for forward compatibility with future ICD
# minor versions that may add new enum values.
_POSITION_BY_VALUE = {
    0: "utility",
    1: "generator",
    2: "transferring",
    3: "unknown",
}
_MODE_BY_VALUE = {
    0: "auto",
    1: "manual",
    2: "test",
    3: "unknown",
}

# Time-skew threshold per ICD §11.
_TIME_SKEW_THRESHOLD_S = 5.0


@dataclass
class AtsSnapshot:
    """Live snapshot of the ATS-Pi's reported state.

    Populated by AtsService.on_poll. Consumed by GenWatch's status
    API, WebSocket push, and the StateMachine's loadSource precedence
    rule. All fields are last-known-values — a None or "unknown"
    indicates the field hasn't been polled successfully yet.
    """

    # Core state (ICD §5.1)
    position: str = "unknown"
    normal_available: bool | None = None
    emergency_available: bool | None = None
    engine_start_calling: bool | None = None
    ats_mode: str = "unknown"
    fault_codes: set[str] = field(default_factory=set)

    # Timestamps (ICD §5.2) — None == register read as 0 (never observed)
    last_transfer_to_gen_ts: float | None = None
    last_retransfer_to_util_ts: float | None = None
    ats_pi_uptime_s: int = 0
    ats_pi_wallclock: float | None = None

    # Counters (ICD §5.3)
    transfer_count_lifetime: int = 0
    transfer_count_24h: int = 0

    # Identification (ICD §5.4)
    icd_version: tuple[int, int] = (0, 0)
    ats_pi_fw: tuple[int, int, int] = (0, 0, 0)
    ats_pi_unit_id: int = 0

    # Command read-back (ICD §5.5)
    cmd_test_active: bool = False
    cmd_inhibit_active: bool = False
    cmd_force_transfer_active: bool = False
    cmd_bypass_delay_active: bool = False

    # Bookkeeping
    last_reading_ts: float = 0.0
    comms: CommsHealth = field(default_factory=CommsHealth)
    # Set to True once we have actually completed a base-tier poll
    # (timestamps + identification populated). Until then,
    # `is_authoritative()` returns False so the loadSource fallback
    # is in effect — even if the prime-tier `position` is already
    # populated. This avoids racing the ICD-version check.
    base_poll_completed: bool = False


class AtsService:
    """Polls the ATS-Pi, maintains the AtsSnapshot, emits transition events.

    Constructed in the GenWatch lifespan (main.py) only when
    `settings.ats.enabled` is true. Wired as the Poller callback for the
    ATS-Pi register map. Holds references to db / bus / slack so it can
    persist events and forward them — same pattern as StateMachine.
    """

    def __init__(
        self,
        regmap: RegisterMap,
        db: Database,
        bus: "EventBus",
        slack: "SlackNotifier | None" = None,
        expected_unit_id: int | None = None,
    ):
        self.regmap = regmap
        self.db = db
        self.bus = bus
        self.slack = slack
        self.expected_unit_id = expected_unit_id
        self.snap = AtsSnapshot()
        # Used to detect ATS-Pi reboots (uptime jumping backwards).
        self._prev_uptime_s: int = 0
        # Used to emit a TIME_SKEW alarm only once per skew transition.
        self._time_skew_alarm_active: bool = False
        # Tracks whether we've ever successfully validated the ICD
        # version — guards against repeated error logs on every poll.
        self._icd_version_validated: bool = False

    # ─── Authority gate ────────────────────────────────────────────────

    def is_authoritative(self) -> bool:
        """True iff the ATS-Pi's `position` should drive the operator-
        visible loadSource. False causes the StateMachine to fall back
        to its H-100-derived value.

        Conditions per ICD §10:
          - comms healthy
          - ICD major version matches what this consumer was built for
          - at least one base poll has completed (so ICD version is real)
          - expected_unit_id, if configured, matches the device
        """
        if self.snap.comms.state != "healthy":
            return False
        if not self.snap.base_poll_completed:
            return False
        if self.snap.icd_version[0] != EXPECTED_ICD_MAJOR:
            return False
        if (
            self.expected_unit_id is not None
            and self.snap.ats_pi_unit_id != self.expected_unit_id
        ):
            return False
        return True

    # ─── Poller callback ───────────────────────────────────────────────

    async def on_poll(
        self,
        tier: str,
        reading: Reading,
        comms: CommsHealth,
    ) -> None:
        """Called by the Poller after each successful (or failed) poll.

        Decodes register values into the snapshot, diffs against the
        previous snapshot to detect transitions, persists events to the
        DB, publishes them on the bus, and forwards to Slack.

        Exceptions inside event handling are caught and logged — a
        downstream failure (DB write, Slack queue full) MUST NOT stop
        the polling loop or affect generator monitoring.
        """
        try:
            await self._update(tier, reading, comms)
        except Exception as e:  # noqa: BLE001
            log.exception("ATS poll handler crashed: %s", e)

    async def _update(
        self,
        tier: str,
        reading: Reading,
        comms: CommsHealth,
    ) -> None:
        emitted: list[dict[str, Any]] = []
        now = time.time()
        prev = self.snap

        # Decode the registers we care about. Missing keys default to
        # the existing snapshot value — important because prime-tier
        # polls don't carry base-tier registers and vice versa.
        v = reading.values

        # Core state — these are in the prime tier
        position_raw = _u16(v, "position", prev_raw=_invert_position(prev.position))
        position = _POSITION_BY_VALUE.get(position_raw, "unknown")
        normal_avail = _bool_or_none(v.get("normal_available"))
        emerg_avail = _bool_or_none(v.get("emergency_available"))
        engine_call = _bool_or_none(v.get("engine_start_calling"))
        mode_raw = _u16(v, "ats_mode", prev_raw=_invert_mode(prev.ats_mode))
        mode = _MODE_BY_VALUE.get(mode_raw, "unknown")
        fault_raw = int(v.get("fault_summary", 0) or 0)
        fault_codes = _decode_fault_bits(fault_raw, self.regmap)

        # Base tier registers — only refreshed on the slower cadence,
        # but persisted on the snapshot between polls.
        last_xfer = _epoch_or_none(v.get("last_transfer_to_gen_ts"))
        last_retxfer = _epoch_or_none(v.get("last_retransfer_to_util_ts"))
        uptime_s = int(v.get("ats_pi_uptime_s", prev.ats_pi_uptime_s) or 0)
        wallclock = _epoch_or_none(v.get("ats_pi_wallclock"))
        count_lifetime = int(v.get("transfer_count_lifetime", prev.transfer_count_lifetime) or 0)
        count_24h = int(v.get("transfer_count_24h", prev.transfer_count_24h) or 0)
        icd_major = int(v.get("icd_version_major", prev.icd_version[0]) or 0)
        icd_minor = int(v.get("icd_version_minor", prev.icd_version[1]) or 0)
        fw_major = int(v.get("ats_pi_fw_major", prev.ats_pi_fw[0]) or 0)
        fw_minor = int(v.get("ats_pi_fw_minor", prev.ats_pi_fw[1]) or 0)
        fw_patch = int(v.get("ats_pi_fw_patch", prev.ats_pi_fw[2]) or 0)
        unit_id = int(v.get("ats_pi_unit_id", prev.ats_pi_unit_id) or 0)

        cmd_test = _bool(v.get("cmd_test_active"), prev.cmd_test_active)
        cmd_inhibit = _bool(v.get("cmd_inhibit_active"), prev.cmd_inhibit_active)
        cmd_force = _bool(v.get("cmd_force_transfer_active"), prev.cmd_force_transfer_active)
        cmd_bypass = _bool(v.get("cmd_bypass_delay_active"), prev.cmd_bypass_delay_active)

        # ── Detect transitions ────────────────────────────────────────

        # Position
        if position != prev.position:
            self._emit_position(prev.position, position, now, emitted)

        # Source availability
        if normal_avail is not None and normal_avail != prev.normal_available and prev.normal_available is not None:
            self._emit_source(
                source="normal",
                available=normal_avail,
                now=now,
                emitted=emitted,
            )
        if emerg_avail is not None and emerg_avail != prev.emergency_available and prev.emergency_available is not None:
            self._emit_source(
                source="emergency",
                available=emerg_avail,
                now=now,
                emitted=emitted,
            )

        # ATS mode
        if mode != prev.ats_mode and prev.ats_mode != "unknown":
            self._emit_event(
                emitted,
                ev_type="ats-mode",
                payload={"from": prev.ats_mode, "to": mode},
                severity="info" if mode == "auto" else "warn",
                type_="ATS_MODE",
                message=f"ATS mode: {prev.ats_mode} → {mode}",
                ts=now,
            )

        # Fault bits — diff as a set, emit individual raise/clear events
        new_faults = fault_codes - prev.fault_codes
        cleared_faults = prev.fault_codes - fault_codes
        for code in sorted(new_faults):
            ab = next((a for a in self.regmap.alarm_bits if a.code == code), None)
            desc = ab.desc if ab else code
            self.db.raise_alarm(code, desc, "warn", 0)
            self._emit_event(
                emitted,
                ev_type="alarm",
                payload={"code": code, "desc": desc, "severity": "warn"},
                severity="warn",
                type_="ALARM",
                message=f"ATS fault raised — {desc}",
                meta=f"code {code}",
                ts=now,
            )
        for code in sorted(cleared_faults):
            ab = next((a for a in self.regmap.alarm_bits if a.code == code), None)
            desc = ab.desc if ab else code
            self.db.clear_alarm(code)
            self._emit_event(
                emitted,
                ev_type="alarm-cleared",
                payload={"code": code, "desc": desc},
                severity="ok",
                type_="ALARM",
                message=f"ATS fault cleared — {desc}",
                meta=f"code {code}",
                ts=now,
            )

        # ATS-Pi reboot detection — uptime going backwards from a known
        # non-zero baseline. We always update _prev_uptime_s afterwards
        # so a single reboot only fires one event (otherwise the next
        # poll, with uptime still small, would re-trigger).
        if self._prev_uptime_s > 0 and uptime_s < self._prev_uptime_s:
            log.warning(
                "ATS-Pi reboot detected — uptime went from %d s to %d s",
                self._prev_uptime_s, uptime_s,
            )
            self._emit_event(
                emitted,
                ev_type="ats-reboot",
                payload={"prev_uptime_s": self._prev_uptime_s, "new_uptime_s": uptime_s},
                severity="info",
                type_="ATS_REBOOT",
                message=f"ATS-Pi rebooted (uptime {self._prev_uptime_s} → {uptime_s} s)",
                ts=now,
            )
        self._prev_uptime_s = uptime_s

        # Time skew (ICD §11)
        if wallclock is not None:
            skew_s = abs(wallclock - now)
            if skew_s > _TIME_SKEW_THRESHOLD_S and not self._time_skew_alarm_active:
                self._time_skew_alarm_active = True
                self.db.raise_alarm(
                    "ATS_PI_TIME_SKEW",
                    f"ATS-Pi wall-clock differs from GenWatch by {skew_s:.1f} s",
                    "warn",
                    0,
                )
                log.warning("ATS-Pi time skew: %.1f s", skew_s)
            elif skew_s <= _TIME_SKEW_THRESHOLD_S and self._time_skew_alarm_active:
                self._time_skew_alarm_active = False
                self.db.clear_alarm("ATS_PI_TIME_SKEW")
                log.info("ATS-Pi time skew cleared")

        # Comms transition — emit the same shape of event StateMachine
        # emits for the H-100 comms link, but tagged so the UI can
        # distinguish which link transitioned.
        if comms.state != prev.comms.state:
            self.db.write_event(
                severity="warn" if comms.state != "healthy" else "ok",
                type_="ATS_COMMS",
                message=f"ATS-Pi comms {comms.state} · {comms.success_pct:.1f}% success",
                meta=None,
            )
            emitted.append({
                "type": "ats-comms",
                "from": prev.comms.state,
                "to": comms.state,
                "successPct": comms.success_pct,
                "ts": now,
            })

        # ICD version validation — log once per session if mismatch
        if not self._icd_version_validated and icd_major > 0:
            if icd_major != EXPECTED_ICD_MAJOR:
                log.error(
                    "ATS-Pi reports ICD major v%d, GenWatch expects v%d. "
                    "is_authoritative() will return False until aligned.",
                    icd_major, EXPECTED_ICD_MAJOR,
                )
            else:
                log.info(
                    "ATS-Pi ICD version OK: v%d.%d (consumer expects v%d.x)",
                    icd_major, icd_minor, EXPECTED_ICD_MAJOR,
                )
            self._icd_version_validated = True

        # ── Update the snapshot ───────────────────────────────────────

        self.snap = AtsSnapshot(
            position=position,
            normal_available=normal_avail if normal_avail is not None else prev.normal_available,
            emergency_available=emerg_avail if emerg_avail is not None else prev.emergency_available,
            engine_start_calling=engine_call if engine_call is not None else prev.engine_start_calling,
            ats_mode=mode,
            fault_codes=fault_codes,
            last_transfer_to_gen_ts=last_xfer if last_xfer is not None else prev.last_transfer_to_gen_ts,
            last_retransfer_to_util_ts=last_retxfer if last_retxfer is not None else prev.last_retransfer_to_util_ts,
            ats_pi_uptime_s=uptime_s,
            ats_pi_wallclock=wallclock if wallclock is not None else prev.ats_pi_wallclock,
            transfer_count_lifetime=count_lifetime,
            transfer_count_24h=count_24h,
            icd_version=(icd_major, icd_minor),
            ats_pi_fw=(fw_major, fw_minor, fw_patch),
            ats_pi_unit_id=unit_id,
            cmd_test_active=cmd_test,
            cmd_inhibit_active=cmd_inhibit,
            cmd_force_transfer_active=cmd_force,
            cmd_bypass_delay_active=cmd_bypass,
            last_reading_ts=now,
            comms=comms,
            base_poll_completed=prev.base_poll_completed or (tier == "base"),
        )

        # ── Publish + forward ─────────────────────────────────────────

        for evt in emitted:
            try:
                await self.bus.publish(evt)
            except Exception as e:  # noqa: BLE001
                log.exception("ats: bus publish failed: %s", e)
            await self._forward_to_slack(evt)

    # ─── Helpers ──────────────────────────────────────────────────────

    def _emit_position(
        self,
        old: str,
        new: str,
        now: float,
        emitted: list[dict[str, Any]],
    ) -> None:
        """Position transition — drives loadSource changes when this
        service is authoritative.

        Suppresses the boot-time `unknown → utility` transition entirely
        (no event, no DB row). The operator doesn't need to see "we
        figured out the load is on utility at startup" — that's just
        the system firming up. The snapshot still updates so the UI
        renders correctly via the normal status push; we just don't
        manufacture a transition event for it.
        """
        if old == "unknown" and new == "utility":
            return

        # Severity: any change involving 'transferring' is informational;
        # restorations to utility are 'ok'; everything pointing at
        # generator is 'warn' (something abnormal is happening).
        if new == "transferring":
            severity = "info"
        elif new == "generator":
            severity = "warn"
        else:  # new == 'utility'
            severity = "ok"

        self._emit_event(
            emitted,
            ev_type="ats-position",
            payload={"from": old, "to": new},
            severity=severity,
            type_="ATS_POSITION",
            message=f"ATS position: {old} → {new}",
            ts=now,
        )

    def _emit_source(
        self,
        source: str,
        available: bool,
        now: float,
        emitted: list[dict[str, Any]],
    ) -> None:
        """Source-availability transition — utility/emergency healthy ↔ lost."""
        if source == "normal":
            if available:
                msg = "Utility power restored"
                severity = "ok"
                code = "UTILITY_RESTORED"
            else:
                msg = "Utility power lost"
                severity = "warn"
                code = "UTILITY_LOST"
        else:  # emergency
            if available:
                msg = "Generator source available"
                severity = "ok"
                code = "GEN_AVAILABLE"
            else:
                msg = "Generator source unavailable"
                severity = "warn"
                code = "GEN_UNAVAILABLE"

        self._emit_event(
            emitted,
            ev_type="ats-source",
            payload={"source": source, "available": available, "code": code},
            severity=severity,
            type_="ATS_SOURCE",
            message=msg,
            meta=code,
            ts=now,
        )

    def _emit_event(
        self,
        emitted: list[dict[str, Any]],
        *,
        ev_type: str,
        payload: dict[str, Any],
        severity: str,
        type_: str,
        message: str,
        ts: float,
        meta: str | None = None,
    ) -> None:
        """Combined DB-event + bus-event emit. Both writes are
        independently exception-safe — a DB failure doesn't block the
        bus publish and vice versa.
        """
        try:
            self.db.write_event(
                severity=severity,
                type_=type_,
                message=message,
                meta=meta,
            )
        except Exception as e:  # noqa: BLE001
            log.exception("ats: db.write_event failed: %s", e)
        evt = {"type": ev_type, "ts": ts, **payload}
        emitted.append(evt)
        log.info("ats event: %s %s", ev_type, payload)

    async def _forward_to_slack(self, evt: dict[str, Any]) -> None:
        """Route ATS events into the existing Slack notifier."""
        if self.slack is None or not self.slack.is_enabled():
            return
        t = evt.get("type")
        ts = float(evt.get("ts") or time.time())
        try:
            if t == "ats-position":
                # Reuse the existing load-source channel — operators
                # care about the *effect* (which source has the load),
                # not the abstract layer that detected it.
                old = str(evt.get("from", ""))
                new = str(evt.get("to", ""))
                # Map ATS position to loadSource vocabulary. The 'transferring'
                # intermediate state is suppressed (typically <2 s).
                if new in ("utility", "generator"):
                    await self.slack.alert_load_source_change(old, new, ts)
            elif t == "ats-comms":
                await self.slack.alert_comms_change(
                    old=str(evt.get("from", "")),
                    new=str(evt.get("to", "")),
                    success_pct=float(evt.get("successPct", 0.0)),
                    ts=ts,
                )
        except Exception as e:  # noqa: BLE001
            log.exception("ats: slack forward failed: %s", e)


# ─── Decoding helpers ────────────────────────────────────────────────────


def _bool(v: Any, default: bool) -> bool:
    if v is None:
        return default
    return bool(int(v))


def _bool_or_none(v: Any) -> bool | None:
    if v is None:
        return None
    return bool(int(v))


def _epoch_or_none(v: Any) -> float | None:
    """Convert a u32 epoch register read to a float timestamp. Per
    ICD §5.2, 0 is the 'never observed' sentinel — return None for that.
    """
    if v is None:
        return None
    iv = int(v)
    return float(iv) if iv > 0 else None


def _u16(v: dict, name: str, prev_raw: int = 0) -> int:
    """Read a u16 register value, defaulting to prev_raw when absent
    (e.g. prime-tier-only read missing a base-tier register).
    """
    val = v.get(name)
    if val is None:
        return prev_raw
    return int(val) & 0xFFFF


def _invert_position(label: str) -> int:
    """Reverse-lookup of position label → enum value, for prev-value
    defaulting. Returns 3 (unknown) for unrecognized labels.
    """
    for k, v in _POSITION_BY_VALUE.items():
        if v == label:
            return k
    return 3


def _invert_mode(label: str) -> int:
    for k, v in _MODE_BY_VALUE.items():
        if v == label:
            return k
    return 3


def _decode_fault_bits(raw: int, regmap: RegisterMap) -> set[str]:
    """Decode the fault_summary bitfield against the alarm_bits rules
    in the ATS-Pi register map.
    """
    active: set[str] = set()
    for ab in regmap.alarm_bits:
        if ab.register == "fault_summary" and (raw & ab.mask) == ab.mask:
            active.add(ab.code)
    return active
