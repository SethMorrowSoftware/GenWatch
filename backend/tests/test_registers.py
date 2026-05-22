from pathlib import Path

import pytest

from genwatch.modbus.registers import (
    RegisterDef,
    batch_reads,
    decode_value,
    load_register_map,
)


@pytest.fixture(scope="module")
def regmap():
    return load_register_map(Path(__file__).parent.parent / "genwatch/registers/h100.yaml")


def test_loads_default_yaml(regmap):
    assert regmap.slave == 100
    assert regmap.read_fc == 3
    assert regmap.prime_poll_ms == 1500
    assert regmap.base_poll_ms == 15000
    assert len(regmap.registers) >= 15
    assert "remote_start" in regmap.controls
    assert "remote_stop" in regmap.controls


def test_register_addresses_are_unique(regmap):
    addrs = [r.addr for r in regmap.registers]
    assert len(addrs) == len(set(addrs)), "duplicate register addresses in yaml"


def test_control_addresses_distinct_from_reads(regmap):
    read_addrs = {r.addr for r in regmap.registers}
    ctl_addrs = {c.addr for c in regmap.controls.values()}
    overlap = read_addrs & ctl_addrs
    assert not overlap, f"control register overlaps with read register: {overlap}"


@pytest.mark.parametrize(
    "type_,words,scale,expected",
    [
        ("u16", [1798], 1.0, 1798),
        ("u16", [139], 0.1, 13.9),
        ("s16", [0xFFFF], 1.0, -1),
        ("s16", [0x8000], 1.0, -32768),
        ("u32", [0, 18476], 0.1, 1847.6),
        ("u32", [1, 0], 1.0, 65536),
        ("s32", [0xFFFF, 0xFFFF], 1.0, -1),
        ("bitfld", [0b1011], 1.0, 0b1011),
        ("enum", [3], 1.0, 3),
    ],
)
def test_decode_value(type_, words, scale, expected):
    r = RegisterDef(name="t", addr=0, type=type_, scale=scale)
    got = decode_value(r, words)
    assert got == pytest.approx(expected)


def test_batch_reads_coalesces_contiguous():
    regs = [
        RegisterDef("a", addr=0x10, type="u16"),
        RegisterDef("b", addr=0x11, type="u16"),
        RegisterDef("c", addr=0x12, type="u16"),
        # 4-word gap allowed
        RegisterDef("d", addr=0x16, type="u16"),
        RegisterDef("e", addr=0x30, type="u32"),  # 2 words
    ]
    batches = batch_reads(regs)
    # a-d coalesce despite the small gap; e is far away
    assert batches == [(0x10, 7), (0x30, 2)]


def test_batch_reads_respects_max_words():
    regs = [RegisterDef(name=f"r{i}", addr=0x100 + i, type="u16") for i in range(80)]
    batches = batch_reads(regs, max_words=64)
    # Should be split into at least two batches
    assert len(batches) >= 2
    for _, count in batches:
        assert count <= 64
