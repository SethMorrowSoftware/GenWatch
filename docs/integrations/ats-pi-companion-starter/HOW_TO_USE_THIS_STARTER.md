# How to move this starter into the `ats-pi-companion` repo

This directory contains the complete starter scaffold for the
`ats-pi-companion` project. It lives inside the GenWatch repo (under
`docs/integrations/ats-pi-companion-starter/`) only because that's
where these files were generated. They are **meant** to be copied
into the empty repo at
<https://github.com/SethMorrowSoftware/ats-pi-companion>.

## One-time setup

```bash
# 1. Clone the empty ats-pi-companion repo somewhere convenient
git clone https://github.com/SethMorrowSoftware/ats-pi-companion.git
cd ats-pi-companion

# 2. Copy this entire starter into the repo root (NOT including this
#    HOW_TO_USE_THIS_STARTER.md or the parent directory itself)
cp -r /path/to/genwatch/docs/integrations/ats-pi-companion-starter/. .
rm HOW_TO_USE_THIS_STARTER.md   # not needed once copied

# 3. Initial commit
git add .
git commit -m "Initial scaffold per ICD v1.0 from GenWatch"
git push origin main
```

## What's in this starter

```
ats-pi-companion-starter/
├── HOW_TO_USE_THIS_STARTER.md   ← this file (don't copy)
├── README.md                     ← top-level intro, ICD link
├── LICENSE                       ← MIT
├── .gitignore
├── pyproject.toml                ← Python packaging
├── config.example.yaml           ← template config
│
├── docs/
│   ├── SPEC.md                   ← detailed implementation spec
│   ├── HARDWARE.md               ← BOM + wiring + install
│   └── DEVELOPMENT.md            ← getting started, testing
│
├── src/atspi/
│   ├── __init__.py               ← version, ICD_VERSION
│   ├── __main__.py               ← CLI entry: `atspi --config ...`
│   ├── config.py                 ← YAML config loader
│   ├── state.py                  ← register store (ICD §5 layout)
│   ├── safety.py                 ← 30 s comms-loss watchdog
│   ├── server.py                 ← Modbus TCP server (pymodbus)
│   ├── io_driver.py              ← abstract I/O interface
│   ├── io_mock.py                ← mock driver (no hardware)
│   └── io_adam.py                ← ADAM-6060 driver (implemented)
│
├── tests/
│   ├── __init__.py
│   └── test_smoke.py             ← imports + basic plumbing
│
└── systemd/
    └── atspi.service             ← production systemd unit
```

## What works out of the box

After copying into the repo, `cd`ing in, and running
`pip install -e ".[dev]"`:

- ✅ `pytest tests/` passes — basic smoke tests for imports, config
  loading, mock driver round-trip, and register store layout
- ✅ `atspi --config config.example.yaml` starts the service with the
  mock I/O driver and the Modbus TCP server
- ✅ `modpoll -m tcp -a 1 -r 1 -c 6 127.0.0.1:502` (or whichever port)
  returns the default healthy state from the mock
- ✅ Writes from `modpoll` round-trip through the read-back registers

## What needs to be implemented (in priority order)

Phases A–F are now implemented and unit-tested; phase G is the
hardware-commissioning task. Breakdown from `docs/SPEC.md §8`:

| Phase | What | Status |
|---|---|---|
| A ✅ | Register store + mock I/O + Modbus server | done |
| B ✅ | Sampling loop wiring + atomic snapshot | done (`__main__.py`, `state.py`) |
| C ✅ | Write command handling — `cmd_test` etc. drive the I/O relays | done; `write_register` queues commands, `_command_loop` drives the driver, read-back reflects driven state (ICD §5.5) |
| D ✅ | Safety watchdog comms-loss auto-release | done (`safety.py`); real-timer test in `tests/test_safety.py` |
| E ✅ | **`io_adam.py` — ADAM-6060 driver** | implemented; **verify the Modbus address map on your unit** (`docs/HARDWARE.md §6`) |
| F ✅ | Persistence of `transfer_count_lifetime` | done (atomic JSON in `state.py`; `tests/test_persistence.py`) |
| G | Production install — systemd, real wiring, ICD §13 golden run | commissioning task (needs hardware) |

## The contract

The **ICD** in the GenWatch repo is the authoritative interface
contract between this project and GenWatch:

→ <https://github.com/SethMorrowSoftware/GenWatch/blob/main/docs/integrations/ats-pi-icd.md>

If you find anything ambiguous or wrong in the ICD while implementing,
**don't work around it silently**. Open a PR against the GenWatch repo
to clarify or fix the ICD first, then update this project to match.
The whole point of the two-repo split is to make the contract explicit;
silently diverging defeats it.

## Coordinating with GenWatch

The GenWatch side of the integration (Phase 1: read-only consumer)
is already shipped. Once this project is running and reachable, set
in GenWatch's `/etc/genwatch/config.yaml`:

```yaml
ats:
  enabled: true
  host: <ats-pi-ip>
  port: 502
  expected_unit_id: 23     # must match this project's site.unit_id config
```

Restart GenWatch. The Live view's HTS-1 card should immediately start
reflecting the position this project publishes. If anything looks off:

- `curl http://localhost:8000/api/status | jq .ats` from the GenWatch
  Pi tells you what GenWatch sees
- `modpoll -m tcp -a 1 -r 1 -c 6 <ats-pi-ip>` from anywhere on the LAN
  tells you what this project is publishing
- Differences between the two ⇒ either a bug here or an ICD ambiguity
