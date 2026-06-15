# ATS-Pi Companion — GenWatch Integration Plan

| | |
|---|---|
| **Version** | 1.1 |
| **Status** | Phases 1-3 **software-complete on both sides** (GenWatch consumer + ATS-Pi companion, validated against mocks); live ATS commands gated only on hardware commissioning (ADAM address-map verification + field wiring + ICD §13 golden run). |
| **Companion document** | [`ats-pi-icd.md`](./ats-pi-icd.md) — the wire contract |
| **Hardware / wiring** | Owned by the companion project — see the [`ats-pi-companion`](https://github.com/SethMorrowSoftware/ats-pi-companion) repo (`HARDWARE.md` + the RS-485 bench-verify guide) |

> **What this document is.** A phased plan for the GenWatch side of the
> ATS-Pi integration. Covers what GenWatch needs to build, in what order,
> with explicit acceptance criteria per phase. Designed so each phase
> lands behind a feature flag and can be reverted in minutes.
>
> **What this document is not.** Not the ICD (that's
> [`ats-pi-icd.md`](./ats-pi-icd.md)). Not the ATS-Pi project's plan —
> the companion team owns their own roadmap. Not a hardware install
> guide (the BOM and field wiring live in the
> [`ats-pi-companion`](https://github.com/SethMorrowSoftware/ats-pi-companion)
> repo; only the *consumer* side is in scope here, since the ATS-Pi team
> owns the physical layer).

---

## 1. Goals

After this plan completes, GenWatch will:

- Display the **authoritative ATS position** (from the ATS-Pi's direct contact sensing) on the Live view, replacing the H-100-derived `loadSource` *when the ATS-Pi link is healthy*, and falling back transparently when it's not.
- Show **source-availability** indicators (Normal Available, Emergency Available) as live chips on the ATS card.
- Log **transfer events** sourced from the ATS-Pi into the existing events feed, alongside H-100 engine-state transitions.
- Provide **operator-issuable commands** (Test, Inhibit, Force Transfer, Bypass Delay) gated by the existing two-step confirm-token flow and role-based authorization.
- Notify Slack on ATS transitions using the existing `alert_on_load_source_change` plumbing.
- Keep working unchanged at sites that have no ATS-Pi (the `ats.enabled` flag defaults to `false`).

---

## 2. Architecture

```
                   ┌─────────────────────────────────────┐
                   │   GenWatch (Raspberry Pi)           │
                   │                                     │
   Generac H-100 ──┼─►  H-100 client + poller            │
   (existing)      │   (existing — no changes)           │
                   │                                     │
   ATS-Pi      ────┼─►  ATS client + poller  ◄──── NEW   │
   (companion)     │   (new — Modbus TCP)               │
                   │                                     │
                   │   ┌─────────────┐                   │
                   │   │ AtsService  │ ◄──── NEW         │
                   │   │ (snapshot + │                   │
                   │   │  events +   │                   │
                   │   │  loadSource │                   │
                   │   │  precedence)│                   │
                   │   └──────┬──────┘                   │
                   │          │                          │
                   │   ┌──────▼──────┐                   │
                   │   │ StateMachine│ ◄──── modified    │
                   │   │ (gen state, │   (loadSource     │
                   │   │  alarms)    │    consumes Ats)  │
                   │   └──────┬──────┘                   │
                   │          │                          │
                   │   FastAPI + WebSocket ──► Browser   │
                   └─────────────────────────────────────┘
```

Key architectural property: **the ATS poller is fully independent of the H-100 poller**. Either one going down does not affect the other. Their data merges in the `AtsService` / `StateMachine` for display purposes only.

---

## 3. Out of scope (for this plan)

- ATS-Pi internals (their project, their concern, gated by the ICD)
- Multi-site federation (single-site only per requirements)
- Building energy metering (no meter installed — deferred)
- Any modifications to the existing H-100 path (`modbus/client.py`, `modbus/poller.py`, `services/control.py` H-100 commands)
- Changes to the existing `loadSource` derivation logic itself — the change is *whether* we use it, not *how* it works

---

## 4. Phases

Each phase is independently shippable and individually revertible (via the `ats.enabled` config flag or by reverting the phase's commit).

### Phase 0 — ICD freeze (no code)

| | |
|---|---|
| **Deliverables** | [`ats-pi-icd.md`](./ats-pi-icd.md) reviewed and signed off by both teams. Register addresses, encodings, semantics finalized. |
| **GenWatch effort** | Review + sign-off only. |
| **Blocker for** | Phase 1 onwards. |
| **Acceptance** | Both teams have committed to the ICD as written or with explicit deltas captured in the doc. |

### Phase 1 — Read-only ATS observation ✅ Shipped

Landed on `main` in commit `4b2583d` (read-only consumer + companion-repo starter). Phase-1 read-only signalling in the UI followed in `9272033`.

| | |
|---|---|
| **Deliverables** | GenWatch can poll an ATS-Pi at a configured `host:port` and expose its read-side state in `GET /api/status` and the WebSocket push. No commands; no UI changes yet. |
| **Feature flag** | `ats.enabled: false` (default). Sites without an ATS-Pi see no change. |
| **GenWatch effort** | 1-1.5 days. |
| **Files added** | `backend/genwatch/services/ats.py` (~150 lines); `backend/genwatch/registers/ats_pi.yaml` (~80 lines); `backend/tests/test_ats_service.py` (~250 lines). |
| **Files modified** | `backend/genwatch/config.py` (+1 model, +1 field); `backend/genwatch/main.py` (lifespan additions, guarded by flag); `backend/genwatch/api/status.py` (new `ats` block when enabled). |
| **Acceptance criteria** | (1) With `ats.enabled: true` and a mock ATS-Pi serving the ICD register layout on `localhost:5020`, `GET /api/status` returns a populated `ats` block. (2) With `ats.enabled: false`, behaviour is byte-identical to the previous release. (3) ATS link going down does not affect the H-100 poller or generator state machine — verified by killing the mock mid-run. (4) `icd_version_major` mismatch logs an error, raises a warn event, and drops the ATS-Pi as authoritative (loadSource falls back to H-100); the ATS poller keeps running and H-100 monitoring is unaffected. (5) New unit tests pass; existing 91 backend tests still pass. |

### Phase 2 — UI surface for read-side data ✅ Shipped

Landed on `main` in commit `a106ef3` (Live view consumes the ATS block). Cross-check between ATS position and H-100 output followed in `d0fe91a` and was refined in audit Batch 1 (PR #37) to avoid false-positives during the ASCO retransfer-delay window.

| | |
|---|---|
| **Deliverables** | Live view shows ATS position from the ATS-Pi when healthy. Source-availability chips. ATS transfer events appear in the events feed. Slack alerts route through the existing `alert_on_load_source_change` channel. |
| **Feature flag** | Same `ats.enabled` flag. UI auto-detects ATS data in the snapshot and adapts. |
| **GenWatch effort** | 1 day. |
| **Files modified** | `frontend/src/types.ts` (add `AtsBlock` type, extend snapshot WS message); `frontend/src/hooks/useLiveData.ts` (merge ATS data from snapshots and `load-source` events); `frontend/src/views/LiveView.tsx` (ATS card additions); `backend/genwatch/services/state.py` (loadSource precedence change). |
| **Acceptance criteria** | (1) With healthy ATS-Pi mock, the HTS-1 pill on the Live view reflects the ATS-Pi's `position` value, not the H-100-derived one. (2) With ATS-Pi unreachable, the badge silently falls back to H-100 derivation and shows a small "(via gen telemetry)" subscript. (3) `UTILITY_LOST` / `UTILITY_RESTORED` events appear in the events feed within one prime-poll cycle of the simulated state change. (4) Slack receives a message on each transition, gated by the existing `alert_on_load_source_change` flag. (5) Frontend `tsc --noEmit` passes; build passes. |

### Phase 3 — Bidirectional commands ✅ Shipped (both sides, software-complete) · ⏳ live use gated on hardware commissioning

**GenWatch consumer side** — implemented and validated against the mock ATS-Pi: `AtsControlService` (`backend/genwatch/services/ats_control.py`), the four endpoints (`backend/genwatch/api/ats.py`), and the Live-view command row + confirm-modal specs. Covered by `backend/tests/test_ats_control.py` and an end-to-end smoke against `MockAtsPiServer` (confirm → command → ICD register write observed in the read-back).

**Companion side** (the [`ats-pi-companion`](https://github.com/SethMorrowSoftware/ats-pi-companion) repo) — software-complete: the hybrid driver (ASCO Group-5 serial sense + ADAM-6060 command relays — `io_hybrid.py` / `io_asco_serial.py` / `io_adam.py`), the write-command dispatch path (a Modbus write drives the relay through the driver; read-back reflects the *driven* state per ICD §5.5), persistence of `transfer_count_lifetime`, and the §8.3 30 s comms-loss auto-release are all implemented and unit-tested (incl. a real-timer auto-release test). An end-to-end smoke drives a command through the real Modbus server → dispatch → driver → read-back.

**Still gated for live use:** the ADAM-6060 Modbus address map must be confirmed against the physical unit's firmware (`HARDWARE.md §6`), the field wiring landed (`HARDWARE.md §3`), and the ICD §13 golden test sequence run against the live ASCO. That's the Phase 4 commissioning work below — no code remains.

| | |
|---|---|
| **Deliverables** | Four new control endpoints (`/api/ats/test`, `/api/ats/inhibit`, `/api/ats/force-transfer`, `/api/ats/bypass-delay`) and matching UI buttons on the Live view, all gated by the existing two-step confirm-token flow. |
| **Feature flag** | Same `ats.enabled` flag. Plus per-command operator/admin role gating: Test / Inhibit / Bypass-delay = operator role; Force-transfer = admin role. |
| **GenWatch effort** | 1 day. |
| **Files added** | `backend/genwatch/api/ats.py` (~120 lines); tests `backend/tests/test_ats_control.py` (~300 lines). |
| **Files modified** | `backend/genwatch/services/control.py` (factor out the confirm-token logic if needed; otherwise add an `AtsControlService` parallel to `ControlService`); `backend/genwatch/main.py` (wire up); `frontend/src/views/LiveView.tsx` (ATS button row); `frontend/src/views/ConfirmModal.tsx` (4 new command specs). |
| **Acceptance criteria** | (1) Each command writes the correct ICD register and the ATS-Pi mock observes the write. (2) Read-back register reflects the command within 500 ms. (3) Force-transfer is rejected for non-admin role with HTTP 403 and audit-logged. (4) ATS comms loss disables all command buttons in the UI and any in-flight command returns 502. (5) The 30-second auto-release safety contract (ICD §8.3) is verifiable: kill GenWatch with `cmd_inhibit_active=1`, wait 35 s, observe the mock has self-released. |

### Phase 4 — Production cutover

| | |
|---|---|
| **Deliverables** | Real ATS-Pi installed and wired at the site. `ats.enabled: true` shipped in production. Staged commissioning checklist completed. |
| **Coordination** | Joint effort with ATS-Pi team. |
| **GenWatch effort** | 0.5 day for commissioning and verification. |
| **Acceptance criteria** | (1) The golden test sequence from ICD §13 runs successfully against the real hardware. (2) A real (or simulated) utility outage produces the expected event chain in GenWatch's events feed within ~15 s. (3) Issuing a Test command from GenWatch causes the ATS to perform a test transfer, observable on the ATS itself and in the resulting events feed. (4) `journalctl -u genwatch --since "1 hour ago"` shows no unexpected errors or comms drops. (5) Operator team has been trained on the new buttons and the precedence rules (ATS-Pi authoritative when healthy). |

---

## 5. GenWatch backend changes — detailed

### 5.1 Config — `backend/genwatch/config.py`

Add a new `AtsConfig` model and a field on `Settings`:

```python
class AtsConfig(BaseModel):
    """ATS-Pi companion device (see docs/integrations/ats-pi-icd.md).

    Disabled by default — sites without an ATS-Pi see no change. When
    enabled, GenWatch starts a second Modbus client and poller targeting
    the configured host.
    """
    enabled: bool = False
    host: str = "192.168.1.250"
    port: int = 5020
    framer: Literal["socket"] = "socket"  # Modbus TCP, NOT RTU
    slave: int = 1
    timeout_s: float = 1.0
    connect_timeout_s: float = 3.0
    register_file: str = "registers/ats_pi.yaml"
    # Site identifier. When set, GenWatch refuses to treat the ATS-Pi as
    # authoritative unless its reported ats_pi_unit_id (register 0x0035)
    # matches — catching a wrong-site cross-wire. Left unset (None) the
    # check is skipped and GenWatch logs a loud startup warning, since it
    # then trusts any ATS-Pi at the configured host IP.
    expected_unit_id: int | None = None


class Settings(BaseSettings):
    # ...
    ats: AtsConfig = Field(default_factory=AtsConfig)
```

And document in `deploy/config.yaml.example`:

```yaml
# ─── ATS-Pi companion device ─────────────────────────────────────────────
# Set enabled: true after the ATS-Pi (see docs/integrations/ats-pi-icd.md)
# is wired and reachable. When disabled, GenWatch falls back to deriving
# load source from H-100 telemetry alone.
ats:
  enabled: false
  host: 192.168.1.250
  port: 5020
  slave: 1
  register_file: registers/ats_pi.yaml
  expected_unit_id: 23           # SITE-23 — see note below; unset skips the guard
```

### 5.2 Register YAML — `backend/genwatch/registers/ats_pi.yaml`

A new register-map YAML mirroring the layout of `h100.yaml` but reflecting the ICD's register map. Skeleton:

```yaml
# GenWatch — ATS-Pi companion register map
# Implements the read side of docs/integrations/ats-pi-icd.md v1.0.

site:
  id: "ATSPI-23"
  name: "ASCO Series 300 (via ATS-Pi)"
  rating_kw: 0           # ATS-Pi doesn't carry the generator rating
  fuel_type: unknown     # not applicable
  tank_gal: 0

modbus:
  slave: 1
  read_fc: 3
  prime_poll_ms: 1500
  base_poll_ms: 15000
  timeout_s: 1.0
  retries: 2

# ─── Position derivation ──────────────────────────────────────────────────
# The ATS-Pi's `position` register IS the position — no derivation needed.
# This rules block is just for documentation; the loadSource consumer in
# AtsService maps the integer enum directly.
ats_position_enum:
  - { value: 0, position: "utility" }
  - { value: 1, position: "generator" }
  - { value: 2, position: "transferring" }
  - { value: 3, position: "unknown" }

# ─── Registers ────────────────────────────────────────────────────────────
registers:
  # Core state (prime tier — 1.5 s)
  - { name: position,            addr: 0x0000, fc: 3, type: u16,    tier: prime, group: "ATS", unit: enum }
  - { name: normal_available,    addr: 0x0001, fc: 3, type: u16,    tier: prime, group: "ATS", unit: bool }
  - { name: emergency_available, addr: 0x0002, fc: 3, type: u16,    tier: prime, group: "ATS", unit: bool }
  - { name: engine_start_calling, addr: 0x0003, fc: 3, type: u16,   tier: prime, group: "ATS", unit: bool }
  - { name: ats_mode,            addr: 0x0004, fc: 3, type: u16,    tier: prime, group: "ATS", unit: enum }
  - { name: fault_summary,       addr: 0x0005, fc: 3, type: bitfld, tier: prime, group: "ATS", unit: bits }

  # Timestamps and counters (base tier — 15 s)
  - { name: last_transfer_to_gen_ts,     addr: 0x0010, fc: 3, type: u32, tier: base, group: "ATS", unit: epoch_s }
  - { name: last_retransfer_to_util_ts,  addr: 0x0012, fc: 3, type: u32, tier: base, group: "ATS", unit: epoch_s }
  - { name: ats_pi_uptime_s,             addr: 0x0014, fc: 3, type: u32, tier: base, group: "ATS", unit: s }
  - { name: ats_pi_wallclock,            addr: 0x0016, fc: 3, type: u32, tier: base, group: "ATS", unit: epoch_s }
  - { name: transfer_count_lifetime,     addr: 0x0020, fc: 3, type: u32, tier: base, group: "ATS", unit: count }
  - { name: transfer_count_24h,          addr: 0x0022, fc: 3, type: u32, tier: base, group: "ATS", unit: count }

  # Identification (base tier)
  - { name: icd_version_major,   addr: 0x0030, fc: 3, type: u16, tier: base, group: "ATS", unit: version }
  - { name: icd_version_minor,   addr: 0x0031, fc: 3, type: u16, tier: base, group: "ATS", unit: version }
  - { name: ats_pi_fw_major,     addr: 0x0032, fc: 3, type: u16, tier: base, group: "ATS", unit: version }
  - { name: ats_pi_fw_minor,     addr: 0x0033, fc: 3, type: u16, tier: base, group: "ATS", unit: version }
  - { name: ats_pi_fw_patch,     addr: 0x0034, fc: 3, type: u16, tier: base, group: "ATS", unit: version }
  - { name: ats_pi_unit_id,      addr: 0x0035, fc: 3, type: u16, tier: base, group: "ATS", unit: id }

  # Command read-back (prime tier — for UI responsiveness)
  - { name: cmd_test_active,           addr: 0x0040, fc: 3, type: u16, tier: prime, group: "ATS", unit: bool }
  - { name: cmd_inhibit_active,        addr: 0x0041, fc: 3, type: u16, tier: prime, group: "ATS", unit: bool }
  - { name: cmd_force_transfer_active, addr: 0x0042, fc: 3, type: u16, tier: prime, group: "ATS", unit: bool }
  - { name: cmd_bypass_delay_active,   addr: 0x0043, fc: 3, type: u16, tier: prime, group: "ATS", unit: bool }

# ─── Fault bits ───────────────────────────────────────────────────────────
# Surfaced via GenWatch's existing alarm pipeline so they appear in the
# events feed and Slack.
alarm_bits:
  - { register: fault_summary, mask: 0x0001, code: "ATS_PI_INPUT_FAULT",  desc: "ATS-Pi input fault — contact stuck",   severity: warn }
  - { register: fault_summary, mask: 0x0002, code: "ATS_PI_OUTPUT_FAULT", desc: "ATS-Pi output fault — relay readback", severity: warn }
  - { register: fault_summary, mask: 0x0004, code: "ATS_PI_MODE_UNKNOWN", desc: "ATS-Pi cannot determine mode",         severity: warn }
  - { register: fault_summary, mask: 0x0008, code: "ATS_PI_CALIBRATION",  desc: "Impossible ATS position — both position-sense contacts asserted; check aux contacts or Group 5 status map", severity: warn }

# ─── Controls (Phase 3) ───────────────────────────────────────────────────
controls:
  - { name: ats_test,                addr: 0x0100, fc: 6, values: [0x0001], desc: "Momentary test transfer" }
  - { name: ats_inhibit_assert,      addr: 0x0101, fc: 6, values: [0x0001], desc: "Inhibit transfer (maintained)" }
  - { name: ats_inhibit_release,     addr: 0x0101, fc: 6, values: [0x0000], desc: "Release inhibit" }
  - { name: ats_force_assert,        addr: 0x0102, fc: 6, values: [0x0001], desc: "Force transfer to gen (maintained)" }
  - { name: ats_force_release,       addr: 0x0102, fc: 6, values: [0x0000], desc: "Release force-transfer" }
  - { name: ats_bypass_delay,        addr: 0x0103, fc: 6, values: [0x0001], desc: "Bypass transfer time delay (momentary)" }
```

### 5.3 AtsService — `backend/genwatch/services/ats.py`

Owns the ATS snapshot, polls callback, transition event emission. Parallel in shape to `StateMachine`:

```python
@dataclass
class AtsSnapshot:
    position: str = "unknown"           # 'utility' | 'generator' | 'transferring' | 'unknown'
    normal_available: bool | None = None
    emergency_available: bool | None = None
    engine_start_calling: bool | None = None
    ats_mode: str = "unknown"
    fault_codes: set[str] = field(default_factory=set)
    last_transfer_to_gen_ts: float | None = None
    last_retransfer_to_util_ts: float | None = None
    transfer_count_24h: int = 0
    transfer_count_lifetime: int = 0
    icd_version: tuple[int, int] = (0, 0)
    ats_pi_uptime_s: int = 0
    comms: CommsHealth = field(default_factory=CommsHealth)


class AtsService:
    """Owns the ATS-Pi snapshot, emits transition events, exposes the
    authoritative `position` for the loadSource precedence rule (see
    docs/integrations/ats-pi-icd.md §10)."""

    def __init__(self, regmap, db, bus, slack):
        self.regmap = regmap
        self.db = db
        self.bus = bus
        self.slack = slack
        self.snap = AtsSnapshot()
        self._last_reboot_uptime: int = 0

    async def on_poll(self, tier, reading, comms):
        # decode values, detect transitions, emit events, persist
        ...

    def is_authoritative(self) -> bool:
        """True if GenWatch should use this service's `position` as the
        `loadSource` displayed to operators. False if the link is
        degraded or the version contract is unsatisfied."""
        return (
            self.snap.comms.state == "healthy"
            and self.snap.icd_version[0] == EXPECTED_ICD_MAJOR
        )
```

### 5.4 StateMachine modification — `backend/genwatch/services/state.py`

Add an optional `ats_service` constructor parameter. Modify `_derive_load_source` so the existing electrical-derivation path is the *fallback*:

```python
class StateMachine:
    def __init__(self, regmap, db, bus, ats_service: AtsService | None = None):
        # ... existing ...
        self.ats = ats_service  # may be None when no ATS-Pi configured

    def _derive_load_source(self, values, engine_state, prev):
        # Authoritative source: ATS-Pi when healthy
        if self.ats is not None and self.ats.is_authoritative():
            return self.ats.snap.position
        # Fallback: existing H-100-electrical derivation
        return self._derive_load_source_from_telemetry(values, engine_state, prev)
```

The existing private method `_derive_load_source_from_telemetry` is the current `_derive_load_source` body, renamed. All existing tests continue to exercise the fallback path unchanged.

### 5.5 Main lifespan — `backend/genwatch/main.py`

Inside `lifespan()`, after the H-100 stack is built and before `yield`, conditionally construct the ATS stack:

```python
ats_service = None
ats_poller = None
ats_client = None

if settings.ats.enabled:
    ats_reg_path = _resolve_register_path(settings.ats.register_file)
    ats_regmap = load_register_map(ats_reg_path)
    ats_client = TcpRtuModbusClient(
        host=settings.ats.host,
        port=settings.ats.port,
        framer=settings.ats.framer,           # 'socket' for Modbus TCP
        timeout_s=settings.ats.timeout_s,
        connect_timeout_s=settings.ats.connect_timeout_s,
        slave=settings.ats.slave,
        retries=ats_regmap.retries,
        backoff_s=ats_regmap.backoff_s,
    )
    await ats_client.connect()  # same stay-up-on-failure behaviour
    ats_service = AtsService(ats_regmap, db, bus, slack)
    ats_poller = Poller(ats_client, ats_regmap, ats_service.on_poll)
    log.info("ATS-Pi integration active — %s:%d slave=%d",
             settings.ats.host, settings.ats.port, settings.ats.slave)

# StateMachine receives ats_service (or None)
state_machine = StateMachine(regmap, db, bus, ats_service=ats_service)

# ... rest of existing setup ...

if ats_poller is not None:
    app.state.ats_service = ats_service
    app.state.ats_poller = ats_poller
    app.state.ats_client = ats_client
    await ats_poller.start()

try:
    yield
finally:
    if ats_poller is not None:
        await ats_poller.stop()
    if ats_client is not None:
        await ats_client.close()
    # ... existing shutdown ...
```

### 5.6 Status API — `backend/genwatch/api/status.py`

Extend the snapshot response with an `ats` block when the service is configured:

```python
# ... existing snap, comms, panel construction ...

ats_block = None
ats_service = getattr(request.app.state, "ats_service", None)
if ats_service is not None:
    s = ats_service.snap
    ats_block = {
        "enabled": True,
        "position": s.position,
        "normalAvailable": s.normal_available,
        "emergencyAvailable": s.emergency_available,
        "engineStartCalling": s.engine_start_calling,
        "atsMode": s.ats_mode,
        "faultCodes": sorted(s.fault_codes),
        "lastTransferToGenTs": s.last_transfer_to_gen_ts,
        "lastRetransferToUtilTs": s.last_retransfer_to_util_ts,
        "transferCount24h": s.transfer_count_24h,
        "transferCountLifetime": s.transfer_count_lifetime,
        "icdVersion": list(s.icd_version),
        "comms": {
            "state": s.comms.state,
            "successPct": s.comms.success_pct,
        },
        "authoritative": ats_service.is_authoritative(),
    }
else:
    ats_block = {"enabled": False}

out["ats"] = ats_block
```

### 5.7 Commands API — Phase 3 — `backend/genwatch/api/ats.py`

Mirrors `api/control.py`. Reuses the existing confirm-token machinery via a parallel `AtsControlService`:

```
POST   /api/ats/test                  operator
POST   /api/ats/inhibit               operator     body: {confirm_token, assert: bool}
POST   /api/ats/force-transfer        admin        body: {confirm_token, assert: bool}
POST   /api/ats/bypass-delay          operator
```

Server-side guards:
- `ats_service.is_authoritative()` must be true (don't issue commands when comms are degraded)
- Force-transfer requires admin role (gated server-side, in addition to UI)
- Force-transfer rejected with 409 if `normal_available == True` (don't force-transfer under healthy utility unless the operator passes an explicit `override: true` confirmation flag — surfaces in the modal copy)

---

## 6. Frontend changes

### 6.1 Types — Phase 1

```typescript
export type AtsPosition = "utility" | "generator" | "transferring" | "unknown";

export interface AtsBlock {
  enabled: boolean;
  position?: AtsPosition;
  normalAvailable?: boolean | null;
  emergencyAvailable?: boolean | null;
  engineStartCalling?: boolean | null;
  atsMode?: "auto" | "manual" | "test" | "unknown";
  faultCodes?: string[];
  lastTransferToGenTs?: number | null;
  lastRetransferToUtilTs?: number | null;
  transferCount24h?: number;
  transferCountLifetime?: number;
  icdVersion?: [number, number];
  comms?: CommsHealth;
  authoritative?: boolean;
}

// Add to StatusBody:
//   ats: AtsBlock;
```

### 6.2 Live view ATS card — Phase 2

Extend the existing `AtsCard` to consume `status.ats` when present. Render:

- The animated SVG diagram now flips based on `status.ats.position` directly (no longer derived from `loadSource`).
- New row of three pill indicators below the diagram:
  - **Normal source:** ✓ Available / ⚠ Unavailable / ? Unknown
  - **Emergency source:** ✓ Available / ⚠ Unavailable / ? Unknown
  - **ATS mode:** AUTO / MANUAL / TEST / UNKNOWN
- "Last transfer" and "Last retransfer" timestamps (relative time: "3 hours ago").
- "Transfers (24h)" counter.
- A small "via ATS-Pi" or "via gen telemetry" annotation showing which path is driving the display.

When `status.ats.enabled === false`, the card behaves exactly as it does today (drives off `status.loadSource`, derived from H-100).

### 6.3 Command buttons — Phase 3

A new button row inside the ATS card, between the diagram and the stats grid:

```
[ Test ] [ Inhibit ] [ Force Transfer ] [ Bypass Delay ]
```

Each opens the existing ConfirmModal with a new command spec. The modal copy must clearly state the consequence of each action — especially Force Transfer, which is admin-gated and must show the override warning when utility is healthy.

---

## 7. Test strategy

### 7.1 Unit tests (per phase)

| Test file | Phase | Coverage |
|---|---|---|
| `test_ats_service.py` | 1 | Position transitions emit correct events; source-availability changes emit correct events; comms loss transitions; ICD version mismatch refuses to mark authoritative; transfer-count 24h counter |
| `test_ats_loadsource_precedence.py` | 2 | ATS-Pi authoritative overrides H-100 derivation; fallback when comms degraded; fallback when ICD major mismatch; existing H-100 derivation untouched (run the existing `test_state_machine.py` suite against a `StateMachine(ats_service=None)`) |
| `test_ats_control.py` | 3 | Each command writes the expected register; role gating (operator vs admin); confirm-token flow; force-transfer healthy-utility guard; comms-loss disables commands |

### 7.2 Mock ATS-Pi for end-to-end test

A standalone Python script (`backend/tests/fixtures/mock_ats_pi.py`) that serves the ICD register layout via pymodbus's TCP server. Can be run during dev:

```bash
python -m tests.fixtures.mock_ats_pi --port 5020
GENWATCH_ATS__ENABLED=true \
GENWATCH_ATS__HOST=127.0.0.1 \
GENWATCH_ATS__PORT=5020 \
genwatch
```

The mock supports a small CLI to drive state transitions for manual testing.

### 7.3 Integration tests

Run the ICD §13 golden test sequence against:
- The mock (during Phase 1 dev)
- A staging ATS-Pi (Phase 3 acceptance)
- The real ATS-Pi during Phase 4 commissioning

---

## 8. Commissioning checklist (Phase 4)

> The full step-by-step field procedure (with bench verification of the
> ADAM relays, safety-gate tests, and sign-off blocks) is in
> [`../COMMISSIONING.md`](../COMMISSIONING.md) Phases 5-9. The checklist
> below is the condensed version.

Before flipping `ats.enabled: true` in production:

- [ ] ATS-Pi physically wired and powered per its team's install doc
- [ ] ATS-Pi reachable from GenWatch Pi: `nc -zv <ats-pi> 5020` returns success
- [ ] `modpoll -m tcp -p 5020 -a 1 -r 1 -c 8 <ats-pi-ip>` returns sensible values (position, source-avails) for the current ATS state
- [ ] ATS-Pi's `icd_version_major` reads `1`
- [ ] ATS-Pi's `ats_pi_unit_id` matches `settings.ats.expected_unit_id`
- [ ] Both Pis show NTP sync: `chronyc tracking` on each, time skew < 5 s (the ICD §9.4 `TIME_SKEW` alarm threshold)
- [ ] Backup of `/etc/genwatch/config.yaml` taken before edit
- [ ] Restart GenWatch, watch `journalctl -u genwatch -f` for clean startup with "ATS-Pi integration enabled" log line
- [ ] `GET /api/status | jq .ats` returns a populated block with `comms.state == "healthy"`
- [ ] Live view shows the ATS card with new indicators
- [ ] Manually trigger a Test command from the UI; observe the ATS perform a test transfer; observe corresponding events in the GenWatch feed
- [ ] Trigger a real or simulated utility outage; observe the full event chain (UTILITY_LOST → engine starts → load on GENERATOR → utility restored → load back on UTILITY → engine cool-down)
- [ ] Slack channel receives all expected notifications
- [ ] Operator team trained on new buttons + precedence semantics

---

## 9. Rollback

Each phase is independently revertible:

- **Phase 1-3 rollback:** set `ats.enabled: false` in `/etc/genwatch/config.yaml`, restart `genwatch.service`. GenWatch reverts to H-100-only operation, identical to behaviour before this plan. No code revert needed.
- **Code revert:** all phases land as separate PRs/commits. Reverting a phase's commit restores the prior behaviour. The flag-gated approach means no flag-disabled code path is ever active in production until that flag flips.
- **Physical rollback:** ATS-Pi hardware can be powered down without affecting the ATS itself or GenWatch's existing H-100 monitoring. The ATS continues to operate from its own automatic logic; GenWatch falls back to H-100-derived `loadSource`.

---

## 10. Acceptance criteria (overall)

The plan is complete when:

1. **All four phases delivered.** Code merged, tests passing, docs updated.
2. **Golden test sequence (ICD §13) passes against real hardware.** Full utility-outage cycle observable in GenWatch's UI and events feed.
3. **Operator-issued Test transfer works end-to-end.** Click → confirm → ATS pulses test input → ATS performs test transfer → GenWatch logs the events → load returns to utility automatically.
4. **No regressions in existing functionality.** All 91+ backend tests still pass. H-100-only sites see no behavioural change.
5. **30-second comms-loss safety auto-release verified on real hardware.** With force-transfer asserted, kill GenWatch; ATS releases within 35 s; ATS-Pi reflects this in the read-back register.
6. **Documentation current.** ICD and this plan reflect the as-built system.

---

## 11. Timeline estimate

| Phase | GenWatch effort | Dependencies |
|---|---|---|
| 0 — ICD freeze | 0.5 d (review) | Companion team sign-off |
| 1 — Read-only consumer | 1-1.5 d | ICD frozen; mock ATS-Pi available |
| 2 — UI surface | 1 d | Phase 1 merged |
| 3 — Commands | 1 d | Phase 2 merged; ATS-Pi write-side implemented |
| 4 — Production cutover | 0.5 d (commissioning) | Phases 1-3 merged; real ATS-Pi installed; site outage window scheduled |
| **Total GenWatch-side** | **~4 days of work**, plus integration / commissioning time |

Companion-team effort (ATS-Pi build) is parallel and out of scope here.

---

## 12. Open items / future work

- **Building energy metering.** When (if) a utility-side meter is installed, extend the ICD with metering registers (kW, kvar, kWh, voltage/frequency). Add a Live view tile and a History dimension. No changes to the v1 contract — the addition would be a v1.x minor bump in the ICD.
- **Multi-site federation.** If GenWatch ever needs to aggregate multiple site/ATS-Pi pairs, the `ats.expected_unit_id` field already provides per-site identity. The current single-site assumption is a UI / config simplification, not a protocol limitation.
- **Group G upgrade migration.** If the site ever upgrades the ASCO Group 5 controller to Group G + 72EE, the ATS-Pi can either (a) remain in place as a Modbus-TCP bridge with the same register layout, or (b) be retired in favour of GenWatch talking directly to the 72EE. Decision deferred.
- **Bypass-isolation switch monitoring.** If a future ATS install includes the bypass-isolation feature (`Y` suffix in the catalog number), additional position contacts on the bypass mechanism could be added to the ICD as new registers in v1.x.

---

*End of plan v1.0.*
