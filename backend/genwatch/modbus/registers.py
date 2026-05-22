"""Register map loader + value decoder.

The YAML schema is documented in genwatch/registers/h100.yaml.

This module is intentionally self-contained: it knows how to turn a list
of raw 16-bit registers fetched from pymodbus into Python values, and
how to group those registers into "prime" / "base" polling batches so
the poller can issue contiguous reads where possible.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml

RegType = Literal["u16", "s16", "u32", "s32", "bitfld", "enum"]
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
        return 2 if self.type in ("u32", "s32") else 1


@dataclass(frozen=True)
class ControlDef:
    name: str
    addr: int
    fc: int = 6
    value: int = 1
    desc: str = ""


@dataclass(frozen=True)
class AlarmCode:
    code: str   # "0x52"
    desc: str
    severity: Literal["alarm", "warn"] = "alarm"

    @property
    def value(self) -> int:
        return int(self.code, 16) if self.code.startswith("0x") else int(self.code)


@dataclass(frozen=True)
class SiteConfig:
    id: str = "SITE-1"
    name: str = "Generac H-100"
    rating_kw: int = 200
    engine: str = "Cummins QSB7-G5"
    tank_gal: int = 220
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
    engine_state_map: dict[int, str]
    alarm_codes: list[AlarmCode]
    registers: list[RegisterDef]
    controls: dict[str, ControlDef]
    raw: dict = field(default_factory=dict)  # for /api/registers GET

    # ---- accessors ----
    def by_name(self, name: str) -> RegisterDef | None:
        return next((r for r in self.registers if r.name == name), None)

    def tier(self, t: RegTier) -> list[RegisterDef]:
        return [r for r in self.registers if r.tier == t]

    def engine_state_for(self, raw: int | None) -> str:
        if raw is None:
            return "unknown"
        return self.engine_state_map.get(raw, "unknown")

    def alarm_for(self, raw: int | None) -> AlarmCode | None:
        if not raw:
            return None
        return next((a for a in self.alarm_codes if a.value == raw), None)


def _coerce_addr(v) -> int:
    if isinstance(v, int):
        return v
    s = str(v)
    if s.lower().startswith("0x"):
        return int(s, 16)
    return int(s)


def _coerce_range(v):
    if not v:
        return None
    if not isinstance(v, (list, tuple)) or len(v) != 2:
        raise ValueError(f"range must be [lo, hi], got {v!r}")
    return (float(v[0]), float(v[1]))


def load_register_map(path: Path | str) -> RegisterMap:
    p = Path(path)
    with p.open() as f:
        data = yaml.safe_load(f) or {}

    site_d = data.get("site") or {}
    ex = (site_d.get("exercise") or {})
    site = SiteConfig(
        id=site_d.get("id", "SITE-1"),
        name=site_d.get("name", "Generac H-100"),
        rating_kw=int(site_d.get("rating_kw", 200)),
        engine=site_d.get("engine", "Cummins QSB7-G5"),
        tank_gal=int(site_d.get("tank_gal", 220)),
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

    esm_raw = data.get("engine_state_map") or {}
    engine_state_map = {int(k): str(v).lower() for k, v in esm_raw.items()}

    alarm_codes = [
        AlarmCode(
            code=str(a["code"]),
            desc=str(a.get("desc", "")),
            severity=str(a.get("severity", "alarm")),
        )
        for a in (data.get("alarm_codes") or [])
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
        d = ControlDef(
            name=c["name"],
            addr=_coerce_addr(c["addr"]),
            fc=int(c.get("fc", 6)),
            value=int(c.get("value", 1)),
            desc=str(c.get("desc", "")),
        )
        controls[d.name] = d

    return RegisterMap(
        path=p,
        site=site,
        slave=slave,
        read_fc=read_fc,
        prime_poll_ms=prime_ms,
        base_poll_ms=base_ms,
        timeout_s=timeout_s,
        retries=retries,
        backoff_s=backoff,
        engine_state_map=engine_state_map,
        alarm_codes=alarm_codes,
        registers=registers,
        controls=controls,
        raw=data,
    )


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
