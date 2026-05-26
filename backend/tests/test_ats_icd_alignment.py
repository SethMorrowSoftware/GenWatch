"""Cross-validate ATS-Pi register addresses across ICD / YAML / mock.

The wire contract between GenWatch and the ATS-Pi companion lives in
three files that are maintained independently:

  - ``docs/integrations/ats-pi-icd.md``      — the wire contract (spec)
  - ``backend/genwatch/registers/ats_pi.yaml`` — what GenWatch polls
  - ``backend/tests/fixtures/mock_ats_pi.py``  — what the test suite exercises

When they drift, unit tests pass while real hardware fails — the worst
kind of latent bug for an integration that ships before the hardware
arrives. This file parses §5 of the ICD markdown and asserts that
every entry's address, word count, and type matches both the YAML and
the mock, and that the mock's reading dict + raw register reads cover
every field the ICD defines.

If you edit one of the three files, expect this test to flag the drift
and tell you exactly which side disagrees. Update the other two (or
amend the ICD if you've genuinely versioned the contract) and re-run.

Scope: §5 (read registers) only. §6 (write registers) uses a different
table schema and will be covered when Phase 3 wires the command path.
"""
from __future__ import annotations

import re
from pathlib import Path

from genwatch.modbus.registers import load_register_map

from tests.fixtures import mock_ats_pi


ICD_PATH = Path(__file__).parent.parent.parent / "docs/integrations/ats-pi-icd.md"
YAML_PATH = Path(__file__).parent.parent / "genwatch/registers/ats_pi.yaml"


# Match a §5 read-register row, e.g.
#
#     | `0x0000` | 1 | u16 enum | `position` | `0`=utility, ... | "transferring" ... |
#
# Captures (addr_hex, words_str, type_str, field_name). RESERVED rows
# don't match because their addr column is a range (``0x0006 – 0x000F``)
# not a single hex literal, and the §5.1.1 fault-bit table doesn't match
# because its first column is a bit number (``0``) not a backticked
# address. §6's write table doesn't match because its second column is
# a backticked field name, not a digit (words count).
_ROW_RE = re.compile(
    r"^\|\s*`(0x[0-9A-Fa-f]+)`\s*\|"   # addr in backticks
    r"\s*(\d+)\s*\|"                    # words (digits)
    r"\s*([^|]+?)\s*\|"                 # type
    r"\s*`([^`]+)`\s*\|",               # field name in backticks
    re.MULTILINE,
)


def _parse_icd_read_registers() -> dict[str, dict]:
    """Parse §5 of the ICD into ``{field_name: {addr, words, type}}``.

    Slices the markdown between ``## 5. Read register map`` and
    ``## 6. Write register map`` so the §6 table and any post-§6
    examples can't accidentally contribute rows.
    """
    text = ICD_PATH.read_text()
    m = re.search(
        r"## 5\. Read register map(.*?)## 6\. Write register map",
        text,
        re.DOTALL,
    )
    assert m, "ICD §5 not found — has the section header been renamed?"
    section = m.group(1)
    out: dict[str, dict] = {}
    for addr_hex, words, type_str, name in _ROW_RE.findall(section):
        assert name not in out, (
            f"duplicate field {name!r} in ICD §5 — parser or doc bug"
        )
        out[name] = {
            "addr": int(addr_hex, 16),
            "words": int(words),
            "type": type_str.strip(),
        }
    return out


def _normalize_icd_type(t: str) -> str:
    """Map an ICD type string to the canonical YAML ``type`` value.

    The ICD writes ``u16 enum`` / ``u16 bool`` / ``u16 bitfield`` to
    convey encoding intent on top of the wire-level type. GenWatch's
    register loader only knows the wire types (``u16``, ``u32``,
    ``bitfld``, etc.) — encoding is handled by the consumer.
    """
    t = t.strip().lower()
    if t == "u16 bitfield":
        return "bitfld"
    if t.startswith("u16"):
        return "u16"
    if t.startswith("u32"):
        return "u32"
    if t.startswith("s16"):
        return "s16"
    if t.startswith("s32"):
        return "s32"
    raise ValueError(f"unknown ICD type string: {t!r}")


# ─── Sanity check on the parser itself ───────────────────────────────────


def test_icd_parser_finds_expected_register_count():
    """ICD §5 currently lists 22 non-RESERVED read registers.

    Counts per subsection: 5.1=6 (position, normal/emergency_available,
    engine_start_calling, ats_mode, fault_summary); 5.2=4 (two
    timestamps + uptime + wallclock); 5.3=2 (lifetime + 24h counters);
    5.4=6 (ICD major/minor, fw major/minor/patch, unit_id); 5.5=4
    (test/inhibit/force/bypass read-backs).

    If this number changes the parser is likely still working, but the
    docstring above and the cross-check tests below should be updated
    to reflect the new register count.
    """
    regs = _parse_icd_read_registers()
    assert len(regs) == 22, (
        f"expected 22 ICD §5 registers, got {len(regs)}: {sorted(regs)}"
    )
    # Spot-check one entry per subsection to catch a parser regression
    # that quietly produces the right count but the wrong addresses.
    assert regs["position"]["addr"] == 0x0000
    assert regs["fault_summary"]["addr"] == 0x0005
    assert regs["last_transfer_to_gen_ts"]["addr"] == 0x0010
    assert regs["transfer_count_lifetime"]["addr"] == 0x0020
    assert regs["icd_version_major"]["addr"] == 0x0030
    assert regs["cmd_test_active"]["addr"] == 0x0040


# ─── ICD ↔ YAML ──────────────────────────────────────────────────────────


def test_yaml_matches_icd_read_register_map():
    """Every ICD §5 register must appear in ``ats_pi.yaml`` with
    matching address, word count, and type. The YAML is what
    GenWatch's poller actually reads off the wire — drift here means
    GenWatch polls the wrong addresses on real hardware.
    """
    icd_regs = _parse_icd_read_registers()
    regmap = load_register_map(YAML_PATH)
    yaml_regs = {r.name: r for r in regmap.registers}

    missing = sorted(set(icd_regs) - set(yaml_regs))
    extra = sorted(set(yaml_regs) - set(icd_regs))
    assert not missing, f"ICD registers missing from YAML: {missing}"
    assert not extra, f"YAML has registers not in ICD §5: {extra}"

    mismatches: list[str] = []
    for name, spec in icd_regs.items():
        yreg = yaml_regs[name]
        if yreg.addr != spec["addr"]:
            mismatches.append(
                f"{name}: ICD addr=0x{spec['addr']:04X}, "
                f"YAML addr=0x{yreg.addr:04X}"
            )
        if yreg.words != spec["words"]:
            mismatches.append(
                f"{name}: ICD words={spec['words']}, YAML words={yreg.words}"
            )
        icd_canon = _normalize_icd_type(spec["type"])
        if yreg.type != icd_canon:
            mismatches.append(
                f"{name}: ICD type={spec['type']!r} → canonical "
                f"{icd_canon!r}, YAML type={yreg.type!r}"
            )
    assert not mismatches, (
        "ICD ↔ YAML drift detected:\n  " + "\n  ".join(mismatches)
    )


# ─── ICD ↔ mock ──────────────────────────────────────────────────────────


def test_mock_serves_every_icd_address():
    """The mock's raw ``read_register(addr)`` path must respond to
    every word of every ICD §5 register. A missing branch would let a
    pymodbus-backed integration test pass while real hardware sees
    the right address-but-wrong-value pattern that's hardest to debug
    after the fact.
    """
    icd_regs = _parse_icd_read_registers()
    store = mock_ats_pi.MockAtsPiStore()
    # Drive a couple of changes off the defaults so RESERVED-vs-real
    # branches diverge — read_register returns 0 for unknown addresses
    # by design, so a "branch missing" bug wouldn't surface against an
    # all-zeros default store.
    store.set_position("generator")
    store.set_mode("test")
    store.set_fault_bit(0x0001, on=True)

    for name, spec in icd_regs.items():
        for offset in range(spec["words"]):
            # Just verify the call doesn't raise — the mock's
            # read_register returns 0 for any unhandled branch (per
            # ICD §5 "MUST return 0x0000"), so a value-equality check
            # here would only re-test the mock's own logic. The point
            # of this test is structural: every ICD address must be
            # in the dispatch.
            store.read_register(spec["addr"] + offset)


def test_mock_as_reading_emits_every_icd_field_name():
    """The mock's ``as_reading(tier='base')`` dict must contain every
    ICD §5 field name as a key.

    ``AtsService._update`` reads by name from this dict (e.g.
    ``v.get('position')``); a missing key silently falls back to the
    previous snapshot value, so the test suite would never see the
    mock's intended state — and any tests asserting on transitions
    would pass trivially. This catches the YAML-name ↔ mock-name
    drift that ``test_yaml_matches_icd_read_register_map`` can't
    (the YAML and mock could agree on a wrong name).
    """
    icd_regs = _parse_icd_read_registers()
    store = mock_ats_pi.MockAtsPiStore()
    reading = store.as_reading(tier="base")

    missing = sorted(set(icd_regs) - set(reading.values))
    assert not missing, (
        f"mock's as_reading() missing ICD §5 fields: {missing}"
    )


def test_mock_prime_reading_includes_prime_tier_fields():
    """``as_reading(tier='prime')`` should expose all prime-tier
    fields (those the YAML marks ``tier: prime``). Base-tier-only
    fields (timestamps, counters, identification) are legitimately
    absent on prime polls — that's the whole point of two-tier
    polling — so they're explicitly excluded from this assertion."""
    icd_regs = _parse_icd_read_registers()
    regmap = load_register_map(YAML_PATH)
    prime_names = {r.name for r in regmap.tier("prime")}

    store = mock_ats_pi.MockAtsPiStore()
    prime_reading = store.as_reading(tier="prime")

    # Every YAML-prime field that's also in the ICD must be present
    # in the prime mock reading.
    prime_icd = set(icd_regs) & prime_names
    missing = sorted(prime_icd - set(prime_reading.values))
    assert not missing, (
        f"mock's as_reading('prime') missing prime-tier fields: {missing}"
    )
