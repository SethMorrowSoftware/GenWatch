"""Persistence (Phase F / ICD §9.3): transfer_count_lifetime MUST survive
a reboot; the transfer timestamps SHOULD. Corrupt/missing state files
fall back to zeros rather than crashing."""
from __future__ import annotations

from atspi.io_driver import InputSnapshot
from atspi.state import ADDR_TRANSFER_COUNT_LIFETIME, RegisterStore

ADDR_LIFETIME_LO = ADDR_TRANSFER_COUNT_LIFETIME + 1  # low word of the u32


def _transfer_to_gen(store: RegisterStore) -> None:
    store.apply_input_snapshot(InputSnapshot(
        position="utility", normal_available=True, emergency_available=True,
        engine_start_calling=False,
    ))
    store.apply_input_snapshot(InputSnapshot(
        position="generator", normal_available=False, emergency_available=True,
        engine_start_calling=True,
    ))


def test_transfer_count_persists_across_restart(tmp_path):
    sf = str(tmp_path / "state.json")
    store = RegisterStore(unit_id=23, state_file=sf)
    _transfer_to_gen(store)
    assert store.read_register(ADDR_LIFETIME_LO) == 1

    # Simulate a reboot: a fresh store reading the same file.
    store2 = RegisterStore(unit_id=23, state_file=sf)
    assert store2.read_register(ADDR_LIFETIME_LO) == 1
    # A second transfer continues the count, not restarts it.
    _transfer_to_gen(store2)
    assert store2.read_register(ADDR_LIFETIME_LO) == 2


def test_command_registers_reset_on_restart(tmp_path):
    # ICD §9.3: command registers MUST come up cleared even though the
    # counter persists.
    sf = str(tmp_path / "state.json")
    store = RegisterStore(unit_id=23, state_file=sf)
    _transfer_to_gen(store)
    store2 = RegisterStore(unit_id=23, state_file=sf)
    assert store2.read_register(0x0041) == 0  # cmd_inhibit read-back
    assert store2.read_register(0x0042) == 0  # cmd_force_transfer read-back


def test_corrupt_state_file_falls_back_to_zero(tmp_path):
    sf = tmp_path / "state.json"
    sf.write_text("{ not valid json")
    store = RegisterStore(unit_id=23, state_file=str(sf))  # must not raise
    assert store.read_register(ADDR_LIFETIME_LO) == 0


def test_no_state_file_still_counts_in_memory():
    store = RegisterStore(unit_id=1)  # state_file=None
    _transfer_to_gen(store)
    assert store.read_register(ADDR_LIFETIME_LO) == 1
