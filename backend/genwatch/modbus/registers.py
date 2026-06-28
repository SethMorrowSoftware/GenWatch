"""Register map loader + value decoder.

The YAML schema is documented in genwatch/registers/h100.yaml.

This module is intentionally self-contained: it knows how to turn a list
of raw 16-bit registers fetched from pymodbus into Python values, and
how to group those registers into "prime" / "base" polling batches so
the poller can issue contiguous reads where possible.

State and alarms on the H-100 are bitfield-derived, not enum-derived.
`derive_engine_state` and `derive_active_alarms` apply the YAML's
`engine_state_bits` and `alarm_bits` rules to a Reading-style dict.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml

RegType = Literal["u16", "s16", "u32", "u32_lo", "s32", "bitfld", "enum"]
RegTier = Literal["prime", "base"]


@dataclass(frozen=True)
class RegisterDef:
    name: str
    addr: int
    fc: int = 3
    type: RegType = "u16"
    tier: RegTier = "base"
    group: str = ""
    unit: str = ""
    scale: float = 1.0
    warn_range: tuple[float, float] | None = None
    alarm_range: tuple[float, float] | None = None

    @property
    def words(self) -> int:
        """How many 16-bit registers this definition consumes."""
        return 2 if self.type in ("u32", "u32_lo", "s32") else 1


@dataclass(frozen=True)
class ControlDef:
    name: str
    addr: int
    fc: int = 6
    value: int = 1
    values: tuple[int, ...] = ()  # FC16 multi-register write; takes precedence over `value`
    desc: str = ""

    @property
    def write_values(self) -> tuple[int, ...]:
        """The actual word sequence to put on the wire."""
        return self.values if self.values else (self.value,)


@dataclass(frozen=True)
class AlarmBit:
    """One alarm bit inside a bitfield register.

    Optional filtering fields:
      - ``suppress_in_states`` — engine-state names in which this alarm
        should be silenced. Useful for alarms that are known firmware
        artifacts in transitional states (e.g. phase-rotation alarms
        during cool-down, when the AVR is dropping out and the H-100's
        phase detector sees the spin-down as a fault). Empty tuple =
        never silenced by state.
      - ``min_poll_count`` — how many consecutive prime polls the bit
        must remain set before the alarm is raised. Debounces transient
        bits (default 1 = raise immediately).
    """
    register: str
    mask: int
    code: str
    desc: str
    severity: Literal["alarm", "warn"] = "alarm"
    suppress_in_states: tuple[str, ...] = ()
    min_poll_count: int = 1


@dataclass(frozen=True)
class StateBitRule:
    """Engine-state derivation rule: if mask is set in `register`, the
    engine is in `state`. First match wins (rules are priority-ordered)."""
    state: str
    register: str
    mask: int


@dataclass(frozen=True)
class PanelModeRule:
    """Panel key-switch derivation rule: if mask is set in `register`,
    the H-100 front-panel key switch is in `mode` (auto / manual / off).
    First match wins; operator commands from this UI are accepted by the
    controller only when mode == 'auto'."""
    mode: str
    register: str
    mask: int


@dataclass(frozen=True)
class SiteConfig:
    id: str = "SITE-1"
    name: str = "Generac H-100"
    rating_kw: int = 200
    engine: str = "Cummins QSB7-G5"
    tank_gal: int = 220
    # Fuel type — 'diesel' | 'gaseous' | 'unknown'. Drives UI gating
    # (hide O₂ sensor card on diesel, etc.). Default 'unknown' so legacy
    # YAML files without the field continue showing every metric.
    fuel_type: str = "unknown"
    exercise_enabled: bool = True
    exercise_day: str = "sun"
    exercise_time: str = "03:00"
    exercise_duration_min: int = 30
    exercise_quiet: bool = True


@dataclass
class RegisterMap:
    path: Path
    site: SiteConfig
    slave: int
    read_fc: int
    prime_poll_ms: int
    base_poll_ms: int
    timeout_s: float
    retries: int
    backoff_s: list[float]
    engine_state_bits: list[StateBitRule]
    alarm_bits: list[AlarmBit]
    panel_mode_bits: list[PanelModeRule]
    registers: list[RegisterDef]
    controls: dict[str, ControlDef]
    raw: dict = field(default_factory=dict)  # for /api/registers GET
    # When true, the state machine raises warn/alarm events from the per-
    # register warn_range/alarm_range bands (a software backstop on top of the
    # H-100's own status bits). Default OFF: the YAML bands ship UNVERIFIED for
    # any given unit, and live alarms from wrong thresholds cause alarm fatigue.
    # Enable per-site only after field-verifying the ranges (see h100.yaml).
    numeric_alarms_enabled: bool = False

    # ---- accessors ----
    def by_name(self, name: str) -> RegisterDef | None:
        return next((r for r in self.registers if r.name == name), None)

    def tier(self, t: RegTier) -> list[RegisterDef]:
        return [r for r in self.registers if r.tier == t]

    # ---- bitfield-based state / alarm derivation ----
    def derive_engine_state(self, values: dict[str, float | int]) -> str:
        """Return the first state whose mask is set, or 'unknown'.

        Rules are evaluated in YAML order. If the referenced register
        hasn't been polled yet, that rule is skipped silently.
        """
        for rule in self.engine_state_bits:
            raw = values.get(rule.register)
            if raw is None:
                continue
            if (int(raw) & rule.mask) == rule.mask:
                return rule.state
        return "unknown"

    def derive_active_alarms(self, values: dict[str, float | int]) -> list[AlarmBit]:
        """Return all alarm bits that are currently set."""
        active: list[AlarmBit] = []
        for ab in self.alarm_bits:
            raw = values.get(ab.register)
            if raw is None:
                continue
            if int(raw) & ab.mask:
                active.append(ab)
        return active

    def derive_panel_mode(self, values: dict[str, float | int]) -> str:
        """Return the first panel mode whose mask is set, or 'unknown'.

        Maps the H-100's key-switch position to a UI label. Returns one
        of 'auto', 'manual', 'off', 'unknown'. Operator commands from
        this UI only engage the controller when mode == 'auto'.
        """
        for rule in self.panel_mode_bits:
            raw = values.get(rule.register)
            if raw is None:
                continue
            if (int(raw) & rule.mask) == rule.mask:
                return rule.mode
        return "unknown"


@dataclass(frozen=True)
class MapValidation:
    errors: list[str]
    warnings: list[str]

    @property
    def ok(self) -> bool:
        return not self.errors


def _coerce_addr(v) -> int:
    if isinstance(v, int):
        return v
    s = str(v)
    if s.lower().startswith("0x"):
        return int(s, 16)
    return int(s)


def _coerce_mask(v) -> int:
    return _coerce_addr(v)


def _load_alarm_bit(a: dict) -> AlarmBit:
    """Parse a single alarm_bits YAML entry, including the optional
    suppress_in_states / min_poll_count filtering knobs.

    Values are normalized:
      - suppress_in_states accepts a list, tuple, or single string;
        every entry is lowercased and stripped of whitespace.
      - min_poll_count is floored at 1 (0 or negative would mean the
        bit fires on the prior poll's value, which is nonsensical).
    """
    suppress = a.get("suppress_in_states") or ()
    if isinstance(suppress, (str, bytes)):
        suppress = (suppress,)
    suppress = tuple(
        str(s).strip().lower()
        for s in suppress
        if str(s).strip()
    )
    try:
        min_polls = int(a.get("min_poll_count", 1))
    except (TypeError, ValueError):
        min_polls = 1
    if min_polls < 1:
        min_polls = 1
    return AlarmBit(
        register=str(a["register"]),
        mask=_coerce_mask(a["mask"]),
        code=str(a["code"]),
        desc=str(a.get("desc", "")),
        severity=str(a.get("severity", "alarm")),
        suppress_in_states=suppress,
        min_poll_count=min_polls,
    )


def _coerce_range(v):
    if not v:
        return None
    if not isinstance(v, (list, tuple)) or len(v) != 2:
        raise ValueError(f"range must be [lo, hi], got {v!r}")
    return (float(v[0]), float(v[1]))


def validate_register_map(rm: RegisterMap) -> MapValidation:
    """Validate safety and structural invariants for a register map."""
    errors: list[str] = []
    warnings: list[str] = []

    if not (1 <= rm.slave <= 247):
        errors.append(f"modbus.slave must be 1..247 (got {rm.slave})")
    if rm.read_fc not in (3, 4):
        errors.append(f"modbus.read_fc must be 3 or 4 (got {rm.read_fc})")

    by_name: set[str] = set()
    occupied_words: dict[int, str] = {}
    for r in rm.registers:
        if r.name in by_name:
            errors.append(f"duplicate register name: {r.name}")
        by_name.add(r.name)

        if r.fc not in (3, 4):
            errors.append(f"register '{r.name}' has unsupported read fc {r.fc} (expected 3/4)")
        if r.tier not in ("prime", "base"):
            errors.append(f"register '{r.name}' has invalid tier '{r.tier}'")
        if r.addr < 0 or r.addr > 0xFFFF:
            errors.append(f"register '{r.name}' addr out of 16-bit range: {r.addr}")

        for w in range(r.addr, r.addr + r.words):
            prior = occupied_words.get(w)
            if prior and prior != r.name:
                errors.append(f"register word overlap at 0x{w:04X}: '{prior}' vs '{r.name}'")
            occupied_words[w] = r.name

    # State/alarm rules must reference real registers
    for rule in rm.engine_state_bits:
        if rule.register not in by_name:
            errors.append(f"engine_state_bits rule references unknown register: {rule.register}")
    for ab in rm.alarm_bits:
        if ab.register not in by_name:
            errors.append(f"alarm_bits rule references unknown register: {ab.register}")
        if ab.mask <= 0 or ab.mask > 0xFFFF:
            errors.append(f"alarm_bits rule '{ab.code}' has invalid mask 0x{ab.mask:X}")
    for pm in rm.panel_mode_bits:
        if pm.register not in by_name:
            errors.append(f"panel_mode_bits rule references unknown register: {pm.register}")
        if pm.mask <= 0 or pm.mask > 0xFFFF:
            errors.append(f"panel_mode_bits rule '{pm.mode}' has invalid mask 0x{pm.mask:X}")
        if pm.mode not in ("auto", "manual", "off"):
            warnings.append(
                f"panel_mode_bits rule has non-standard mode '{pm.mode}' "
                f"(UI expects auto/manual/off; will display as 'unknown')"
            )

    control_names: set[str] = set()
    for c in rm.controls.values():
        if c.name in control_names or c.name in by_name:
            errors.append(f"duplicate control name: {c.name}")
        control_names.add(c.name)
        if c.fc not in (6, 16):
            errors.append(f"control '{c.name}' has unsupported write fc {c.fc} (expected 6/16)")
        if c.addr < 0 or c.addr > 0xFFFF:
            errors.append(f"control '{c.name}' addr out of 16-bit range: {c.addr}")
        if c.addr in occupied_words:
            # Some H-100 registers are dual-purpose: writing triggers an
            # action, reading exposes status (e.g. QUIETTEST_STATUS at
            # 0x022B, ALARM_ACK at 0x012E). Surface as a warning so this
            # is visible in /api/registers/verify but doesn't block load.
            warnings.append(
                f"control '{c.name}' shares address 0x{c.addr:04X} with read register "
                f"'{occupied_words[c.addr]}' (intentional for H-100 dual-purpose registers; "
                f"verify if this control was added recently)"
            )
        if c.fc == 6 and len(c.write_values) != 1:
            errors.append(f"control '{c.name}' fc:6 requires exactly one value (got {len(c.write_values)})")

    if not rm.registers:
        errors.append("register map has no read registers")
    if not rm.controls:
        warnings.append("register map has no control registers")

    return MapValidation(errors=errors, warnings=warnings)


def load_register_map(path: Path | str) -> RegisterMap:
    p = Path(path)
    with p.open() as f:
        data = yaml.safe_load(f) or {}

    site_d = data.get("site") or {}
    ex = (site_d.get("exercise") or {})
    raw_fuel = str(site_d.get("fuel_type", "unknown")).strip().lower()
    fuel_type = raw_fuel if raw_fuel in ("diesel", "gaseous", "unknown") else "unknown"
    site = SiteConfig(
        id=site_d.get("id", "SITE-1"),
        name=site_d.get("name", "Generac H-100"),
        rating_kw=int(site_d.get("rating_kw", 200)),
        engine=site_d.get("engine", "Cummins QSB7-G5"),
        tank_gal=int(site_d.get("tank_gal", 220)),
        fuel_type=fuel_type,
        exercise_enabled=bool(ex.get("enabled", True)),
        exercise_day=str(ex.get("day", "sun")).lower(),
        exercise_time=str(ex.get("time", "03:00")),
        exercise_duration_min=int(ex.get("duration_min", 30)),
        exercise_quiet=bool(ex.get("quiet", True)),
    )

    mb = data.get("modbus") or {}
    slave = int(mb.get("slave", 100))
    read_fc = int(mb.get("read_fc", 3))
    prime_ms = int(mb.get("prime_poll_ms", 1500))
    base_ms = int(mb.get("base_poll_ms", 15000))
    timeout_s = float(mb.get("timeout_s", 1.5))
    retries = int(mb.get("retries", 2))
    backoff = list(mb.get("backoff_s", [0.25, 0.5, 1.0]))

    engine_state_bits = [
        StateBitRule(
            state=str(r["state"]).lower(),
            register=str(r["register"]),
            mask=_coerce_mask(r["mask"]),
        )
        for r in (data.get("engine_state_bits") or [])
    ]

    alarm_bits = [_load_alarm_bit(a) for a in (data.get("alarm_bits") or [])]

    panel_mode_bits = [
        PanelModeRule(
            mode=str(r["mode"]).lower(),
            register=str(r["register"]),
            mask=_coerce_mask(r["mask"]),
        )
        for r in (data.get("panel_mode_bits") or [])
    ]

    registers: list[RegisterDef] = []
    for r in data.get("registers") or []:
        registers.append(
            RegisterDef(
                name=r["name"],
                addr=_coerce_addr(r["addr"]),
                fc=int(r.get("fc", read_fc)),
                type=r.get("type", "u16"),
                tier=r.get("tier", "base"),
                group=str(r.get("group", "")),
                unit=str(r.get("unit", "")),
                scale=float(r.get("scale", 1.0)),
                warn_range=_coerce_range(r.get("warn_range")),
                alarm_range=_coerce_range(r.get("alarm_range")),
            )
        )

    controls: dict[str, ControlDef] = {}
    for c in data.get("controls") or []:
        values_raw = c.get("values")
        if values_raw:
            values_tuple = tuple(_coerce_mask(v) for v in values_raw)
        else:
            values_tuple = ()
        d = ControlDef(
            name=c["name"],
            addr=_coerce_addr(c["addr"]),
            fc=int(c.get("fc", 6)),
            value=int(c.get("value", 1)),
            values=values_tuple,
            desc=str(c.get("desc", "")),
        )
        controls[d.name] = d

    rm = RegisterMap(
        path=p,
        site=site,
        slave=slave,
        read_fc=read_fc,
        prime_poll_ms=prime_ms,
        base_poll_ms=base_ms,
        timeout_s=timeout_s,
        retries=retries,
        backoff_s=backoff,
        engine_state_bits=engine_state_bits,
        alarm_bits=alarm_bits,
        panel_mode_bits=panel_mode_bits,
        registers=registers,
        controls=controls,
        raw=data,
        numeric_alarms_enabled=bool(data.get("numeric_alarms_enabled", False)),
    )
    report = validate_register_map(rm)
    if report.errors:
        raise ValueError("; ".join(report.errors))
    return rm


def decode_value(reg: RegisterDef, words: list[int]) -> float | int | None:
    """Turn raw 16-bit words into the engineering value."""
    if not words:
        return None
    if reg.type == "u16":
        v = words[0] & 0xFFFF
    elif reg.type == "s16":
        v = words[0] & 0xFFFF
        if v & 0x8000:
            v -= 0x10000
    elif reg.type == "u32":
        if len(words) < 2:
            return None
        # Big-endian (Modbus default); high word first
        hi, lo = words[0] & 0xFFFF, words[1] & 0xFFFF
        v = (hi << 16) | lo
    elif reg.type == "u32_lo":
        # 16-bit value carried in the LOW word of a 2-register slot; the
        # high word is reserved (documented zero on the H-100). Read only
        # the low word so a framing slip that leaves garbage in the high
        # word can't blow the value up by ~65536× into the UI or a
        # cross-check. Contrast `u32`, which honours both words for
        # genuine 32-bit counters (e.g. run_hours). When the high word is
        # actually zero — the documented normal case — this is byte-for-
        # byte identical to the old u32 decode, so it's a safe migration.
        if len(words) < 2:
            return None
        v = words[1] & 0xFFFF
    elif reg.type == "s32":
        if len(words) < 2:
            return None
        hi, lo = words[0] & 0xFFFF, words[1] & 0xFFFF
        u = (hi << 16) | lo
        v = u if u < 0x80000000 else u - 0x100000000
    elif reg.type == "bitfld":
        v = words[0] & 0xFFFF
    elif reg.type == "enum":
        v = words[0] & 0xFFFF
    else:
        v = words[0] & 0xFFFF

    if reg.scale != 1.0 and reg.type not in ("bitfld", "enum"):
        return float(v) * reg.scale
    return v


def batch_reads(regs: list[RegisterDef], max_words: int = 64) -> list[tuple[int, int]]:
    """Group registers into contiguous (start_addr, count_words) reads.

    Coalescing reduces round-trip Modbus latency: 8 separate reads at
    ~50 ms each is 400 ms vs ~70 ms for one read of 16 contiguous regs.
    Gaps up to 4 words wide are coalesced too — wasting a few reads is
    cheaper than another RTU round-trip.
    """
    if not regs:
        return []
    sorted_regs = sorted(regs, key=lambda r: r.addr)
    batches: list[tuple[int, int]] = []
    start = sorted_regs[0].addr
    end = start + sorted_regs[0].words  # exclusive
    GAP_TOLERANCE = 4

    for r in sorted_regs[1:]:
        # If r is within gap tolerance and total words stays <= max_words, extend
        new_end = max(end, r.addr + r.words)
        if r.addr - end <= GAP_TOLERANCE and (new_end - start) <= max_words:
            end = new_end
        else:
            batches.append((start, end - start))
            start = r.addr
            end = r.addr + r.words
    batches.append((start, end - start))
    return batches
