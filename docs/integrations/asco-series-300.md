# Integrating an ASCO Series 300 ATS into GenWatch — Path B (Modbus I/O Island)

Step-by-step integration of an ASCO Series 300 Power Transfer Switch
(Group 5 controller, 473670-006, 600 A / 480 V / 3-phase) into the
existing GenWatch monitoring stack, **without** replacing the legacy ATS
controller.

This guide implements **Path B** from the feasibility analysis: keep the
Group 5 controller in place and bridge the ATS's existing dry-contact
I/O onto a small Modbus TCP I/O module ("I/O island") on the same LAN
as the Lantronix bridge. GenWatch then talks to the ATS exactly the
same way it already talks to the H-100: as another Modbus device with
its own YAML register map.

> **Audience.** A field technician for the wiring sections (§3–§4) and a
> Python developer for the backend sections (§5–§7). Sections are
> tagged accordingly. All work in the ATS enclosure requires a
> qualified electrician — 480 V at 600 A is lethal.

---

## 0. Background and scope

### What you get
- **Source availability** (Normal Available, Emergency Available)
- **Switch position** (load currently on Normal vs Emergency)
- **Transfer event detection** (with timestamps in GenWatch's event log)
- **Remote commands**: Momentary Test, Inhibit Transfer, Bypass Time
  Delay, Maintained Transfer to Emergency
- **Slack alerts** on utility loss / restore / transfer
- **History timeline** entries correlated with generator state

### What you do *not* get (Group 5 limitation)
- Voltage / current / frequency / power readings from the ATS — the
  Group 5 controller does not expose these on any external interface.
  The H-100's electrical metering still covers the generator side.
- Time-delay setpoints over the wire (still adjusted on the controller
  faceplate).
- Event log on the ATS controller itself (not exposed).

If any of these are required, use Path A instead (Group G retrofit
P/N 955717-001 + 72EE Quad-Ethernet module, full Modbus TCP).

### Architectural fit
GenWatch today wires one `ModbusClient` → one `Poller` → one
`StateMachine` → one set of API/WS endpoints (see
`backend/genwatch/main.py` lifespan). Path B adds a **second,
independent stack** for the ATS: a second client, a second poller, a
new `AtsService`, and an `ats` block on existing API responses. The
two stacks share the event bus, the database, and the Slack notifier.

```
                ┌─────────────────────────────┐
                │   GenWatch (Raspberry Pi)   │
                │                             │
   ┌──────────► │ Poller A → StateMachine     │
   │  Modbus    │           (H-100 engine)    │
   │  RTU/TCP   │                             │
   │            │ Poller B → AtsService       │ ◄────┐
   │            │           (ASCO ATS)        │      │
   │            │                             │      │  Modbus
   │            │ FastAPI + WebSocket ──► UI  │      │  TCP
   │            └─────────────────────────────┘      │  (MBAP)
   │                                                 │
┌──┴────────────┐                              ┌─────┴────────┐
│  Lantronix    │                              │ ADAM-6060    │
│  UDS/EDS      │                              │ (I/O island) │
└──────┬────────┘                              └──────┬───────┘
       │ RS-232                                       │ dry contacts
       ▼                                              ▼
┌───────────────┐                              ┌──────────────────┐
│ Generac H-100 │                              │ ASCO Series 300  │
│   controller  │                              │ Group 5 panel    │
└───────────────┘                              │ (473670-006)     │
                                               └──────────────────┘
```

---

## 1. Bill of materials

### Recommended kit (assumes 18RX and one aux contact set are already in the ATS)

| # | Part | Qty | Approx. cost (USD) | Purpose |
|---|------|-----|--------------------|---------|
| 1 | Advantech **ADAM-6060** (6 DI + 6 relay outputs, Modbus TCP) | 1 | $400 | The I/O island |
| 2 | Mean Well **DR-30-24** (30 W, 24 VDC DIN-rail PSU) | 1 | $50 | Powers the ADAM |
| 3 | DIN-rail (35 mm), end stops, and ferrules | 1 lot | $40 | Mounting |
| 4 | Cat6 patch cable (length to suit) + RJ45 surge protector (Ubiquiti ETH-SP-G2) | 1 | $40 | LAN drop |
| 5 | 22 AWG stranded control wire (red/black/blue/grey/etc.) | 1 spool | $30 | Field wiring |
| 6 | Wire markers / sleeves | 1 set | $20 | Traceability |
| 7 | 5×20 mm fuse holder + 1 A SB fuse on 24 VDC supply | 1 | $15 | PSU protection |

**Estimated total (with accessories already installed): ~$600.**

### Accessory kits — add these if missing (see §2 survey)

| Part | Qty | Approx. cost | Notes |
|------|-----|--------------|-------|
| ASCO **18RX REX module** (kit 935148) — adds RL5 (Emergency Available) and RL6 (Normal Available) | 1 | $400–600 | Often present from factory on 600 A units. |
| ASCO **14AA / 14BA aux contact kit** — adds spare position-indication contacts | 1 | $200–400 | One set is typically standard. A second set lets you sense Normal and Emergency independently. |

> If the unit was ordered without these, lead time can be 2–6 weeks from
> ASCO. Order before scheduling the integration outage.

### Alternative I/O modules

The integration is module-agnostic at the protocol layer; any Modbus TCP
discrete I/O module with ≥6 DI and ≥4 DO will work. Other field-proven
options:

- **ADAM-6050** — 12 DI + 6 DO (more spare DI channels)
- **Sealevel SeaIO-N 110** — US-made, similar form factor
- **Acromag 989EN-4016** — 16 DIO, configurable per channel
- **Moxa ioLogik E1212** — 8 DI + 8 DO/relay
- **Wago 750-880 PFC** + 750-432 (DI) + 750-516 (DO) — modular, pricier

Pick by what's already standardized at your sites. The YAML map in
§5.1 must be adapted to the module's register map either way.

---

## 2. Pre-installation survey

Before ordering anything, open the ATS enclosure with the unit locked
out and confirm what's actually installed.

### 2.1 Inventory the controller's optional accessories

Look for the following inside the cabinet, adjacent to the Group 5
control panel and along the switch mechanism:

| Item | What it looks like | Where |
|------|--------------------|-------|
| **18RX REX module** | Small relay board (about 3" × 2") with terminals labelled RL5/RL6 and a green LED | Mounted near the controller, often on a sub-bracket |
| **14AA / 14BA aux contacts** | Small auxiliary contact blocks clamped onto the main switch mechanism, with field wires running back to a separate terminal strip | On the switch mechanism, not on the controller |
| **Engine-start wires** | Two field wires running from the ATS engine-start terminals (typically TB labelled "3", "4") out of the cabinet to the generator's H-100 remote-start input | Usually the lowest field terminal strip |

Photograph each terminal strip and label every wire before you touch
anything. Match each wire to the operator's manual **381333-289**
field-wiring section.

### 2.2 Confirm physical fit

- [ ] At least 6" of free DIN-rail space inside the enclosure (or
      adjacent gutter) for the I/O module and PSU.
- [ ] 120 VAC control power available — check for a spare position on
      the existing control transformer's secondary, or plan to add a
      branch from the cabinet's 120 VAC bus.
- [ ] Ethernet drop reachable. If the cabinet is outside, you need an
      outdoor-rated Cat6 run with surge protection at both ends. Keep
      Ethernet >12" away from any 480 V wiring and cross at 90°, never
      parallel.

### 2.3 Confirm LAN reachability

The I/O module must be reachable from the Pi running GenWatch.
Easiest configuration: put it on the **same subnet and VLAN as the
Lantronix bridge** (typically the OT VLAN). Reserve a static IP in your
DHCP server — recommend `192.168.1.250` if the Lantronix is `.249`.

### 2.4 Decision checkpoint

If §2.1 shows the 18RX and at least one aux contact set are missing:
**stop, order the kits, install at the next planned outage, then
resume**. Trying to integrate without source-availability signals
collapses the value of Path B to almost nothing — you'd see
load-disconnect pulses but couldn't tell *why* the switch transferred.

---

## 3. Electrical wiring

**Field technician section.** All steps assume the cabinet is de-energized
under lockout/tagout per NFPA 70E and your site's electrical safety
program. Both **utility** and **generator** sources must be locked out.

### 3.1 Mount the I/O module and PSU

1. Snap the 24 VDC PSU and the ADAM-6060 onto a clean section of DIN
   rail with end stops at both ends.
2. Bring 120 VAC L/N/G from a fused branch (1 A) to the PSU input.
3. Land 24 VDC + / − from the PSU output to the ADAM's Vs+ / GND
   terminals. **Do not** parallel the ADAM's GND with the cabinet
   chassis ground at multiple points — single-point bond only, at the
   PSU GND terminal.
4. Land the chassis safety ground from the DIN rail to the cabinet
   ground bus.
5. Connect the Cat6 patch from the ADAM's LAN port through the surge
   protector, then out of the cabinet to the LAN.

### 3.2 Discrete-input wiring (read from ATS)

The ADAM-6060 DI channels are dry-contact sense: connect one side of
the dry contact to the DI channel, the other side to DI.COM.

| ADAM DI | From ATS terminal | Signal | Active state |
|---------|-------------------|--------|--------------|
| DI0 | Load Disconnect (T1 ↔ T2) | Switch transferring | Pulses closed for ~1 s during a transfer |
| DI1 | Aux 14AA — Normal position N/O contact | "On Normal" | Closed when load is on Normal |
| DI2 | Aux 14BA — Emergency position N/O contact | "On Emergency" | Closed when load is on Emergency |
| DI3 | 18RX RL6 (Normal Available) N/O contact | Normal source healthy | Closed when Normal is acceptable |
| DI4 | 18RX RL5 (Emergency Available) N/O contact | Emergency source healthy | Closed when Emergency is acceptable |
| DI5 | *(spare — recommended: ATS engine-start contact, in parallel with the existing wire to the H-100)* | Engine-start asserted by ATS | Closed when ATS is calling for engine start |

> **Why DI5 is worth wiring.** Without it, you can't distinguish a
> generator start initiated by the ATS (utility failed, automatic
> transfer in progress) from one initiated by an operator via
> GenWatch's existing Start button. Logging both gives you a clean
> audit trail.

### 3.3 Relay-output wiring (drive ATS inputs)

The ADAM-6060 relay outputs are Form A dry contacts. Wire each one
across the corresponding ATS input pair. These are 5 VDC / 5 mA logic
inputs — well within the relay's contact-life curve, but note the
*minimum* switching current spec on the ADAM (typically 10 mA at
5 VDC); add a 10 kΩ "wetting" resistor in parallel with the ATS input
if you observe intermittent activation during commissioning.

| ADAM DO | To ATS terminal | Command | Pulse type |
|---------|-----------------|---------|------------|
| DO0 | Momentary Test Switch (T6 ↔ T7) | Initiate test transfer | Momentary (≥500 ms) |
| DO1 | Maintained Transfer to Emergency (T8 ↔ T9) | Force transfer to Emergency, hold while closed | Maintained |
| DO2 | Inhibit Transfer to Emergency (T10 ↔ T11) | Block automatic transfer (maintenance) | Maintained |
| DO3 | Bypass Transfer Time Delay (T12 ↔ T13) | Skip the configured TD on the next transfer | Momentary |
| DO4 | *(spare — Engine Exerciser T4 ↔ T5 if you want GenWatch to drive it instead of the H-100)* | Start unloaded engine exercise | Maintained |
| DO5 | *(spare)* | — | — |

> **Do not** wire DO5 to T14/T15/T16 (factory-use terminals).

### 3.4 Wire labelling and bend radius

- Every field wire ferruled at both ends, labelled with both endpoints
  (e.g. `ADAM DI3 ← ATS 18RX RL6 NO`).
- Maintain a service loop at the ADAM so the module can be unclipped
  from the rail without putting tension on landings.
- Keep low-voltage signal wires bundled together and physically
  separated from any 120 VAC / 480 V conductors.

### 3.5 Re-energize and walk-down

1. Remove all LOTO devices per procedure.
2. Re-energize the cabinet.
3. With the ATS controller in its normal AUTO state, visually verify
   the ADAM's power LED, LAN link LED, and at least one of the
   "source available" DI LEDs are illuminated. (Both DI3 and DI4 should
   be illuminated if both sources are present — which they will be, if
   the generator was already running for some reason — otherwise just
   DI3.)

---

## 4. I/O module configuration

### 4.1 Initial network setup

ADAM-6060 ships with default IP `10.0.0.1`. Configure via Advantech
**Adam/Apax .NET Utility** (Windows) or via the module's built-in web
UI on the default IP:

1. Set static IP `192.168.1.250` (or your reservation), mask
   `255.255.255.0`, gateway/DNS as appropriate.
2. Set a non-default password on the web UI.
3. Enable **Modbus TCP** on port `502`. Disable any unused services
   (ASCII command server, etc.).
4. Set Modbus unit ID to `1` (default).
5. Optional: set the digital-input filter time to 10 ms to debounce
   the Load Disconnect pulse on DI0.

### 4.2 Modbus register map (ADAM-6060)

Advantech publishes the full map in the *ADAM-6000 Series User Manual*.
The relevant addresses are:

| Function | Modbus range | FC | Notes |
|----------|--------------|----|----|
| DI status (per-bit) | 00001–00006 (coils) | 02 (read input discrete) | One coil per DI |
| DO status (read-back) | 00017–00022 (coils) | 01 (read coil) | |
| DO control (write) | 00017–00022 (coils) | 05 (write single) / 15 (write multi) | |
| All DI packed into one word | 40001 (holding) | 03 / 04 | Bits 0..5 = DI0..DI5 |
| All DO packed into one word | 40002 (holding) | 03 / 04 | Bits 0..5 = DO0..DO5 (read-back) |
| DO control (per-bit holding) | 00017+ via FC06 | 06 | Vendor extension; many ADAM models support this |

**Recommendation: use the packed holding-register form (40001 / 40002).**
This matches the existing GenWatch register-decoder schema — `bitfld`
type, FC03 — with no decoder changes. Verify against the specific
firmware revision on your unit.

> Read addresses above as Modbus PDU offsets: `00001` → PDU `0x0000`,
> `40001` → PDU `0x0000`. The ADAM web UI shows the human-readable form
> with the leading function digit, the YAML below uses the PDU form.

### 4.3 Bench test before integration

From the Pi (or any Linux host on the LAN), confirm Modbus TCP works:

```bash
# Install modpoll once (Debian/Raspbian)
sudo apt install -y modpoll

# Read the packed DI register (40001, 1 word, slave 1)
modpoll -m tcp -a 1 -r 1 -c 1 192.168.1.250

# Set DO0 high (write coil 17 = TRUE)
modpoll -m tcp -a 1 -r 17 -t 0 192.168.1.250 1

# Set DO0 low
modpoll -m tcp -a 1 -r 17 -t 0 192.168.1.250 0
```

Cross-check the LED on the ADAM and the corresponding ATS behaviour.
Do **not** energize the Maintained Transfer (DO1) at this stage with
real load present — see §6 commissioning.

---

## 5. GenWatch backend changes

**Developer section.** All paths are relative to the repository root.
Estimated effort: **2–3 days** for backend, **1 day** for frontend.

### 5.1 New register YAML — `backend/genwatch/registers/asco_300_io.yaml`

A new YAML file mirroring the schema of `h100.yaml`, but with
ATS-specific derivation rules. Create as a sibling file:

```yaml
# GenWatch — ASCO Series 300 ATS via ADAM-6060 I/O island
#
# Wired per docs/integrations/asco-series-300.md §3.2 / §3.3.
# Verify addresses against your ADAM-6060 firmware (User Manual,
# "Modbus Mapping Table") — packed forms are at 0x0000 / 0x0001.

site:
  id: SITE-23-ATS
  name: "ASCO Series 300 — 600A 480V"
  switch_type: "Group 5 Controller"
  cat_no: "J00300030600N1X0"

modbus:
  slave: 1
  read_fc: 3
  prime_poll_ms: 500           # ATS state must be snappy — TX events are brief
  base_poll_ms: 5000           # no slow-moving telemetry, so don't need 15s
  timeout_s: 1.0
  retries: 2
  backoff_s: [0.1, 0.25, 0.5]

# ─── ATS position derivation ──────────────────────────────────────────────
# First match wins. Both Normal and Emergency aux contacts may briefly
# be open simultaneously during the transition — we report "transferring"
# rather than "unknown" in that window.
ats_position_bits:
  - { position: "normal",       register: di_packed, mask: 0x0002 }   # DI1
  - { position: "emergency",    register: di_packed, mask: 0x0004 }   # DI2
  - { position: "transferring", register: di_packed, mask: 0x0001 }   # DI0 Load Disconnect

# ─── Source availability ──────────────────────────────────────────────────
source_state_bits:
  - { source: "normal_available",     register: di_packed, mask: 0x0008 }   # DI3
  - { source: "emergency_available",  register: di_packed, mask: 0x0010 }   # DI4
  - { source: "engine_start_active",  register: di_packed, mask: 0x0020 }   # DI5 (optional)

# ─── Alarm-equivalent events ──────────────────────────────────────────────
# Modeled as alarms so they reuse the existing StateMachine.raise_alarm
# / clear_alarm machinery and surface in the Events log + Slack.
alarm_bits:
  - { register: di_packed, mask: 0x0008, code: "UTILITY_LOST",   desc: "Normal source not available",     severity: alarm, invert: true }
  - { register: di_packed, mask: 0x0010, code: "GEN_NOT_READY",  desc: "Emergency source not available",  severity: warn,  invert: true }

registers:
  # Prime · 0.5 s — the packed DI word drives everything
  - { name: di_packed,  addr: 0x0000, fc: 3, type: bitfld, tier: prime, group: "ATS", unit: bits }
  - { name: do_packed,  addr: 0x0001, fc: 3, type: bitfld, tier: prime, group: "ATS", unit: bits }

controls:
  - { name: ats_test,           addr: 0x0010, fc: 6, values: [0x0001], pulse_ms: 1000, desc: "Momentary test transfer (DO0)" }
  - { name: ats_transfer,       addr: 0x0011, fc: 6, values: [0x0001], desc: "Maintained transfer to Emergency (DO1)" }
  - { name: ats_transfer_clear, addr: 0x0011, fc: 6, values: [0x0000], desc: "Release maintained transfer (DO1)" }
  - { name: ats_inhibit,        addr: 0x0012, fc: 6, values: [0x0001], desc: "Inhibit transfer (DO2)" }
  - { name: ats_inhibit_clear,  addr: 0x0012, fc: 6, values: [0x0000], desc: "Release inhibit (DO2)" }
  - { name: ats_bypass_delay,   addr: 0x0013, fc: 6, values: [0x0001], pulse_ms: 500,  desc: "Bypass transfer time delay (DO3)" }
```

> **Schema additions vs h100.yaml:** `ats_position_bits`,
> `source_state_bits`, `invert: true` on alarm bits, and a new
> `pulse_ms` field on controls (for momentary writes). These require
> small extensions to `RegisterMap` in `modbus/registers.py` — see §5.4.

### 5.2 Config schema — `backend/genwatch/config.py`

Add a new Pydantic model and a top-level field:

```python
class AtsConfig(BaseModel):
    """ASCO Series 300 ATS via Modbus TCP I/O island (Path B).

    Disabled by default — sites without an ATS see no change in
    behaviour. When enabled, GenWatch starts a second Modbus client and
    poller targeting the I/O module configured below.
    """
    enabled: bool = False
    host: str = "192.168.1.250"
    port: int = 502
    framer: Literal["rtu", "socket"] = "socket"  # ADAM-6060 = real Modbus/TCP
    slave: int = 1
    timeout_s: float = 1.0
    connect_timeout_s: float = 3.0
    register_file: str = "registers/asco_300_io.yaml"


class Settings(BaseSettings):
    # ...existing fields...
    ats: AtsConfig = Field(default_factory=AtsConfig)
```

And add a corresponding block in `deploy/config.yaml.example`:

```yaml
# ─── ASCO Series 300 ATS via Modbus TCP I/O island ───────────────────────
# Set enabled: true after wiring the ADAM-6060 (or equivalent) per
# docs/integrations/asco-series-300.md. Disabled = no second poller is
# started; existing single-device operation is unchanged.
ats:
  enabled: false
  host: 192.168.1.250
  port: 502
  framer: socket            # Modbus/TCP (MBAP). 'rtu' only if RTU-over-TCP.
  slave: 1
  register_file: registers/asco_300_io.yaml
```

### 5.3 Lifespan wiring — `backend/genwatch/main.py`

Inside `lifespan()`, after the existing H-100 client/poller are
constructed but **before** `yield`, add the ATS stack guarded by
`settings.ats.enabled`. Keep the two stacks fully independent so an
ATS failure cannot affect generator monitoring.

Sketch:

```python
ats_client: ModbusClient | None = None
ats_poller: Poller | None = None
ats_service: AtsService | None = None

if settings.ats.enabled:
    ats_reg_path = _resolve_register_path(settings.ats.register_file)
    ats_regmap = load_register_map(ats_reg_path)
    ats_client = TcpRtuModbusClient(
        host=settings.ats.host,
        port=settings.ats.port,
        framer=settings.ats.framer,           # 'socket' for ADAM-6060
        timeout_s=settings.ats.timeout_s,
        connect_timeout_s=settings.ats.connect_timeout_s,
        slave=settings.ats.slave,
        retries=ats_regmap.retries,
        backoff_s=ats_regmap.backoff_s,
    )
    await ats_client.connect()  # same "stay up on failure" semantics
    ats_service = AtsService(ats_regmap, db, bus, slack)
    ats_poller = Poller(ats_client, ats_regmap, ats_service.on_poll)
    app.state.ats_regmap = ats_regmap
    app.state.ats_client = ats_client
    app.state.ats_service = ats_service
    app.state.ats_poller = ats_poller
    await ats_poller.start()
    log.info("ATS integration active — %s:%d slave=%d",
             settings.ats.host, settings.ats.port, settings.ats.slave)
else:
    app.state.ats_service = None

try:
    yield
finally:
    # shutdown order: ATS stack first, then H-100
    if ats_poller is not None:
        await ats_poller.stop()
    if ats_client is not None:
        await ats_client.close()
    # ...existing shutdown...
```

> **`TcpRtuModbusClient` already accepts `framer="socket"`** per the
> existing `ModbusTcpConfig` schema (`Literal["rtu", "socket"]`). If a
> code review of the client implementation reveals it hard-codes the
> RTU framer somewhere, factor that out into a `framer` argument rather
> than adding a second client class — the transport machinery is
> otherwise identical.

### 5.4 ATS service — new `backend/genwatch/services/ats.py`

Mirror the shape of `services/state.py`, but with an ATS-specific
snapshot and event vocabulary. The class owns no I/O — the existing
`Poller` calls `on_poll` with each `Reading`.

```python
@dataclass
class AtsSnapshot:
    position: str = "unknown"               # normal | emergency | transferring | unknown
    normal_available: bool | None = None
    emergency_available: bool | None = None
    engine_start_active: bool | None = None
    last_transfer_ts: float | None = None
    last_retransfer_ts: float | None = None
    inhibit_active: bool = False            # mirrors DO2 read-back
    last_reading: Reading = field(default_factory=Reading)
    comms: CommsHealth = field(default_factory=CommsHealth)


class AtsService:
    def __init__(self, regmap, db, bus, slack):
        self.regmap = regmap
        self.db = db
        self.bus = bus
        self.slack = slack
        self.snap = AtsSnapshot()

    async def on_poll(self, tier, reading, comms):
        new_position = self.regmap.derive_ats_position(reading.values)
        sources = self.regmap.derive_source_states(reading.values)
        # ... diff and emit:
        #   UTILITY_LOST / UTILITY_RESTORED on normal_available transitions
        #   GEN_AVAILABLE / GEN_UNAVAILABLE on emergency_available
        #   TRANSFER_TO_EMERGENCY / RETRANSFER_TO_NORMAL on position change
        # ... write events to db, publish on bus, forward to slack
```

Emit the same event-shape as `StateMachine`:

```json
{"type": "ats-transition", "from": "normal", "to": "emergency", "ts": 1717000000.0}
{"type": "ats-source",     "source": "normal", "available": false, "ts": 1717000001.5}
```

This keeps the WebSocket consumer logic simple — the UI just listens
for `ats-*` types and updates its panel accordingly.

### 5.5 New API endpoints — `backend/genwatch/api/ats.py`

Mirror `api/control.py` for the command surface and `api/status.py` for
read-only state.

```
GET    /api/ats/status            → AtsSnapshot serialized
POST   /api/ats/test              → momentary DO0 pulse  (operator)
POST   /api/ats/inhibit           → set DO2 = on         (operator + confirm)
DELETE /api/ats/inhibit           → set DO2 = off        (operator)
POST   /api/ats/bypass-delay      → momentary DO3 pulse  (operator)
POST   /api/ats/transfer          → set DO1 = on         (admin + confirm)
DELETE /api/ats/transfer          → set DO1 = off        (admin)
```

All write endpoints reuse the existing `ControlService` confirm-token
pattern and write to the audit log. Maintained Transfer is **admin
only**, gated behind a confirm token, *and* checks that
`normal_available == False` before honouring (an operator force-transfer
under healthy utility power is a foot-gun).

Extend `GET /api/status` to include an `ats:` block:

```json
{
  "engine": { ...existing... },
  "ats": {
    "enabled": true,
    "position": "normal",
    "normalAvailable": true,
    "emergencyAvailable": false,
    "inhibitActive": false,
    "comms": { "state": "healthy", "successPct": 100.0 },
    "lastTransferTs": null,
    "lastRetransferTs": null
  }
}
```

When `ats.enabled: false`, return `"ats": {"enabled": false}` only.

### 5.6 WebSocket extension — `backend/genwatch/api/ws.py`

Extend the existing snapshot push (built in `main.py:on_poll`) to merge
the ATS snapshot alongside the engine snapshot, and to publish ATS
events on the same bus. No new socket endpoint is needed.

### 5.7 Database, events, and Slack

**No schema migration is needed.** Reuse the existing `events` table
with new `type_` values: `UTILITY`, `TRANSFER`, `ATS_COMMS`,
`ATS_CONTROL`. The `alarms` table can carry `UTILITY_LOST` and
`GEN_NOT_READY` as active-alarm rows so they appear in the existing
Active Alarms widget.

In `services/slack.py`, add one new helper method
`alert_ats_transition(from, to, ts)` that posts a standard-format
message. Existing flags (`alert_on_state_change`, `alert_on_command`)
gate it; or add `alert_on_ats_transition: bool = True` to `SlackConfig`
if you want it independently controllable.

---

## 6. Frontend changes

### 6.1 Types — `frontend/src/types.ts`

```ts
export type AtsPosition = "normal" | "emergency" | "transferring" | "unknown";

export interface AtsSnapshot {
  enabled: boolean;
  position?: AtsPosition;
  normalAvailable?: boolean;
  emergencyAvailable?: boolean;
  inhibitActive?: boolean;
  comms?: CommsHealth;
  lastTransferTs?: number | null;
  lastRetransferTs?: number | null;
}
```

### 6.2 New `AtsPanel` component — `frontend/src/components/AtsPanel.tsx`

Place it on the Live view, beside the existing engine status card. Show:

- Big position pill: **ON NORMAL** (green) / **ON EMERGENCY** (amber) /
  **TRANSFERRING** (blinking blue) / **UNKNOWN** (grey)
- Two source-availability chips: Normal ✅ / Emergency ⚠
- Inhibit indicator (red banner if active)
- Action buttons (gated on auth role + ATS comms healthy):
  - Test transfer (operator)
  - Inhibit / Release inhibit (operator)
  - Bypass delay (operator)
  - Maintained transfer (admin, confirm modal)

### 6.3 History timeline

Add ATS events to the existing events list with a distinct icon and
colour so an outage timeline reads at a glance: utility lost →
generator started (H-100 event) → transfer to emergency (ATS event) →
utility restored → retransfer (ATS event) → generator cooldown → stop.

### 6.4 Hide gracefully when disabled

If `ats.enabled: false`, return `null` from `<AtsPanel>` so existing
sites without an ATS see no UI change.

---

## 7. Commissioning — staged bring-up

Do **not** wire and energize everything at once. Each stage adds one
layer of risk; verify before proceeding.

### Stage 1 — I/O module alone (no ATS wiring)
- DO **not** land any wires on ATS terminals yet.
- Power the ADAM, confirm web UI reachable.
- Run the `modpoll` checks from §4.3. Verify each relay output clicks
  audibly when toggled.
- ✅ Pass criterion: bidirectional Modbus TCP communication from Pi.

### Stage 2 — Read-only DI wiring
- Land DI0–DI4 (and DI5 if used) on the ATS source terminals.
- Leave **all DO landings open-circuit** (relay outputs disconnected).
- With the ATS in normal operation (load on Normal, both sources
  available), confirm DI3 (Normal Avail) and DI1 (On Normal) are HIGH;
  DI2 and DI4 LOW.
- Pull the generator's local Stop on the H-100 (so Emergency goes
  unavailable). Confirm DI4 goes LOW. Restart, confirm DI4 returns
  HIGH after a few seconds of stabilization.
- ✅ Pass criterion: every DI changes in step with the corresponding
  physical condition.

### Stage 3 — GenWatch backend integration, no commands
- Deploy backend with `ats.enabled: true`, but **comment out** all DO
  control endpoints in `api/ats.py` (or guard with a feature flag).
- Restart `genwatch.service`, verify in the journal that the ATS poller
  starts and reports `comms healthy`.
- Confirm `GET /api/status` returns sensible `ats:` data.
- Confirm WebSocket pushes include ATS state.
- ✅ Pass criterion: 30 minutes of clean ATS telemetry with no
  spurious alarms or comms drops.

### Stage 4 — DO commands, open-circuit
- Land the DO leads onto **terminal blocks not yet jumpered to the
  ATS** — i.e. the ADAM relay closes a circuit that goes nowhere.
- Exercise each command endpoint. Confirm the audit log records the
  write and the ADAM's relay LED toggles, but **nothing happens at
  the ATS**.
- ✅ Pass criterion: every command logs an audit row with the issuing
  operator, the action, and the resulting Modbus write.

### Stage 5 — Connect DO commands to ATS, one at a time
- Jumper **DO3 (Bypass Time Delay)** first — it's the least
  consequential.
- Issue the command from the UI. Then issue **Test** (DO0). The ATS
  should run a test transfer with no TD: utility-side voltage drops to
  the load momentarily, generator starts (via existing H-100
  engine-start wire), load picks up on emergency.
- Land Inhibit (DO2) next, confirm transfer is blocked while engaged.
- Land Maintained Transfer (DO1) **last** — coordinate with site
  occupants as this WILL transfer load.
- ✅ Pass criterion: each command produces the expected ATS behaviour
  and GenWatch logs the resulting transition events with timestamps
  inside 2 s of the physical event.

### Stage 6 — End-to-end utility loss simulation
- With site notified and a scheduled outage window:
- Open the utility breaker upstream of the ATS.
- Observe: GenWatch logs `UTILITY_LOST` within 1 s, then the H-100's
  existing `STATE→cranking→running` sequence, then `TRANSFER_TO_EMERGENCY`.
- Close the utility breaker. Observe: `UTILITY_RESTORED`, then
  `RETRANSFER_TO_NORMAL` after the ATS's retransfer time delay,
  followed by H-100 cooldown and stop.
- ✅ Pass criterion: the full event chain is captured in the Events log
  and posted to Slack with correct ordering and timestamps.

---

## 8. Test plan

### Automated tests (new)

| File | Coverage |
|------|----------|
| `backend/tests/test_ats_registers.py` | YAML loads, position derivation, source derivation, alarm derivation with `invert: true` |
| `backend/tests/test_ats_service.py` | Transition emission, alarm raise/clear, debounce on transferring state, comms-lost behaviour |
| `backend/tests/test_ats_api.py` | All endpoints with operator vs admin auth, confirm-token flow, the "force-transfer under healthy utility" rejection |

Extend `MockModbusClient` to optionally serve a second register map
(for the ATS island) — the existing mock pattern uses one regmap per
client, so two mock instances cover the dual-stack case.

### Manual hardware tests
- Source-loss debounce: brief utility brown-out (<1 s) should not
  cause spurious `UTILITY_LOST` events. Use the DI filter time on the
  ADAM if needed.
- Reconnect storm: cycle the ADAM's Ethernet 10 times. GenWatch must
  recover automatically each time without restarting the service.
- Backplane isolation: cut power to the H-100 Modbus link only;
  confirm ATS telemetry continues uninterrupted (and vice versa).

---

## 9. Rollback procedure

The integration is **non-destructive to ATS function**. If anything
goes wrong:

1. **Soft disable**: set `ats.enabled: false` in
   `/etc/genwatch/config.yaml` and `sudo systemctl restart genwatch`.
   The second poller never starts; the H-100 stack is unchanged.
2. **Open the DO landings** at the ATS terminal strip. The ATS now
   sees no remote commands; it operates from its own front-panel
   controls only.
3. **Power down the ADAM**. The ATS is fully independent of the
   integration — its core transfer logic is in the Group 5 controller
   firmware and has never depended on any of this wiring.

The ATS itself is never reconfigured, reflashed, or modified by this
integration. All changes are external wire landings on the existing
field terminals.

---

## 10. Known limitations and future work

- **Per-phase utility metering** is not available. If you need it, add
  a separate revenue-grade meter (e.g. Schneider PM5560) on the ATS
  Normal side and poll it as a third Modbus device.
- **Transfer time delays** must still be set on the controller
  faceplate. There is no remote configuration path on Group 5.
- **Event correlation** between the ATS and the H-100 currently
  happens in the UI (by adjacent timestamps). A future enhancement
  could promote this to a backend "incident" object that groups all
  events of an outage into one record.
- **Bypass-isolation switches** — if your cabinet has the bypass
  feature (catalog number ends in `Y` instead of `N`), additional
  position contacts may be available on the bypass mechanism. Out of
  scope for this guide.
- **Path A migration**: if the Group 5 controller fails or is
  end-of-lifed by ASCO, this integration is forward-compatible — when
  you swap to a Group G controller with a 72EE module, you can keep
  the same `ats:` config block, change `register_file` to a new
  Group-G YAML, and inherit all the API/UI/Slack plumbing built here.

---

## Appendix A — ADAM-6060 quick reference

| Spec | Value |
|------|-------|
| Power | 10–30 VDC (24 VDC nom.) |
| DI | 6× isolated, sink/source, 10 ms default filter |
| DO | 6× Form A relay, 5 A @ 250 VAC / 30 VDC |
| Protocol | Modbus TCP (port 502) + Advantech ASCII |
| Default IP | 10.0.0.1 |
| Default unit ID | 1 |
| Manual | "ADAM-6000 Series User Manual", Advantech, latest revision |

## Appendix B — ASCO Series 300 terminal block (from your unit)

| Terminal | Function | Type | Rating |
|----------|----------|------|--------|
| 1, 2, 3 | Load Disconnect Contacts | Output (Form C dry) | 120 VAC / 5 A |
| 4, 5 | Optional Engine Exerciser | Input | 5 VDC / 5 mA |
| 6, 7 | Momentary Test Switch | Input | 5 VDC / 5 mA |
| 8, 9 | Maintained Transfer to Emergency | Input | 5 VDC / 5 mA |
| 10, 11 | Inhibit Transfer to Emergency | Input | 5 VDC / 5 mA |
| 12, 13 | Bypass Transfer Time Delay | Input | 5 VDC / 5 mA |
| 14, 15, 16 | Factory use — **do not wire** | — | — |

Plus, on the switch mechanism / sub-bracket:

| Accessory | Contact | Function |
|-----------|---------|----------|
| 14AA (standard) | N/O, N/C | Position-Normal indication |
| 14BA (optional) | N/O, N/C | Position-Emergency indication |
| 18RX RL5 | Form C | Emergency Source Available |
| 18RX RL6 | Form C | Normal Source Available |
| Engine-start TB (separate) | Form A | Calls generator when neither source available or during test |

## Appendix C — References

- ASCO Series 300 Operator's Manual 381333-289 (your unit's manual,
  per nameplate).
- *User's Guide — Group 5 Controller / 7000 Series Operator's Manual*,
  381333-126K.
- *ASCO 5100 Series 5140 Quad-Ethernet Module manual*, 381333-417.
- *ASCO 300 Series Spec Sheet (Group G Controller)*, ASCO/Schneider.
- Advantech *ADAM-6000 Series User Manual*.
- pymodbus documentation — `framer` parameter on `AsyncModbusTcpClient`.
- Sister doc: feasibility comparison of Paths A / B / C (in the
  original integration discussion thread; consolidate into this `docs/`
  folder when the next path is documented).
