# ATS-Pi ↔ GenWatch — Interface Control Document (ICD)

| | |
|---|---|
| **Version** | 1.0 |
| **Status** | **Signed off** — wire contract verified compatible end-to-end (GenWatch consumer ↔ ATS-Pi companion). |
| **Effective** | 2026-06-15 |
| **Owner (GenWatch side)** | GenWatch project |
| **Owner (ATS-Pi side)** | (TBD — companion project team) |
| **Supersedes** | All prior direct-integration approaches (a direct ADAM-6060 I/O island, or a Group-G + 72EE retrofit). The ATS-Pi companion is the single supported integration. |

> **What this document is.** A frozen contract between two independently
> developed projects — GenWatch (generator monitor) and ATS-Pi (transfer-
> switch companion) — that share a single LAN. It specifies exactly what
> the ATS-Pi must expose over Modbus TCP, how GenWatch will consume it,
> and how the two systems behave when one of them is degraded or absent.
>
> **What this document is not.** It is not a project plan (see
> `ats-pi-plan.md`), nor an implementation guide for either side. It is
> the wire-level and semantic contract only.

---

## 1. Purpose and scope

The single-site deployment has:
- A Generac H-100 controller on the generator (already monitored by GenWatch over Modbus-RTU-over-TCP via a Lantronix bridge — unchanged by this ICD).
- An ASCO Series 300 automatic transfer switch (Group 5 controller, P/N 473670-006, 600 A / 480 V / 3-φ).
- A new dedicated Raspberry Pi (the **ATS-Pi**) that senses the ASCO's state from the Group 5 controller over RS-485 serial and drives the ASCO's command inputs through an ADAM-6060 relay module, exposing the ASCO's state on the LAN.

This ICD covers:
- The Modbus TCP wire protocol between the ATS-Pi and GenWatch
- The complete read and write register map (the "shared address space")
- Encoding conventions, units, sentinel values
- Semantic guarantees both sides must uphold
- Failure modes and how each side degrades gracefully
- Versioning rules for future expansion

Out of scope:
- The ATS-Pi's internal architecture (its OS, language, I/O hardware choice — all up to the ATS-Pi team, as long as the wire contract below is honoured)
- GenWatch's internal architecture (the consumer side is documented in `ats-pi-plan.md`)
- Building-side energy metering (deferred until a meter is installed)

---

## 2. Architecture overview

```
                          ┌─────────────┐
                          │   ASCO 300  │
                          │   ATS       │
                          │  (Group 5)  │
                          └──┬──────────┘
                             │ ATS-Pi senses + commands the ASCO:
                             │   • Group 5 status over RS-485 serial —
                             │     position, source availability,
                             │     engine-start (sense)
                             │   • test / inhibit / force-transfer /
                             │     bypass-delay via ADAM-6060 relays (drive)
                             │
                          ┌──▼──────────┐
                          │   ATS-Pi    │   ← owned by companion-project team
                          │  (companion │
                          │   project)  │
                          └──┬──────────┘
                             │
                             │  Modbus TCP, port 5020
                             │  unit ID 1, this ICD's register map
                             │
                  ──────────────────────────────  OT VLAN
                                ▲
                                │
                          ┌──────────┐
                          │ GenWatch │  ← owned by GenWatch project
                          │   Pi     │
                          └────┬─────┘
                               │
            Modbus-RTU-over-TCP│      (Lantronix bridge,
            to Generac H-100   │       unchanged)
```

**Role separation:**
- **ATS-Pi** is the *only* device that physically touches the ASCO. It owns all wiring into the ATS, all debouncing of physical contacts, and all timing of command pulses. From GenWatch's perspective, the ATS-Pi is an opaque Modbus device.
- **GenWatch** is a *consumer* of the ATS-Pi's exposed state. It polls, displays, logs events on state changes, and issues commands. GenWatch never writes directly to the ASCO.

**Implication:** the entire physical layer (contact debounce, pulse timing, electrical isolation) is the ATS-Pi's responsibility. Bugs there don't surface as GenWatch bugs.

**Driver-agnostic.** How the ATS-Pi obtains the ASCO's state internally is an ATS-Pi implementation detail. It serves the identical register map regardless; GenWatch cannot tell how the state was sensed and does not need to. (The deployed companion uses a **hybrid** driver — Group 5 status over RS-485 for sensing, ADAM-6060 relays for the command outputs — but the wire contract below is the only thing GenWatch depends on.)

---

## 3. Transport

| Parameter | Value | Notes |
|---|---|---|
| Protocol | **Modbus TCP** (MBAP-framed) | *Not* Modbus-RTU-over-TCP. Use the `socket` framer in pymodbus terms. |
| Server | ATS-Pi | Listens on the configured port |
| Client | GenWatch | Opens a long-lived TCP connection |
| Port | **5020** | The ATS-Pi runs as a non-root service and binds a high port (no `CAP_NET_BIND_SERVICE` / no root), so **5020 is the default and recommended value**. Configurable on the ATS-Pi; GenWatch's `ats.port` MUST match whatever the companion is set to. |
| Unit ID | **1** | The ATS-Pi exposes one unit. |
| Read functions accepted | **FC03** (read holding registers), **FC04** (read input registers — same address space, identical content) | GenWatch will use FC03. FC04 support is optional but recommended for diagnostic tools. |
| Write functions accepted | **FC06** (write single register), **FC16** (write multiple registers) | All defined commands fit in single-register writes; FC16 is permitted for future multi-word commands. |
| Connection model | **Single persistent connection** from GenWatch; the ATS-Pi MAY accept additional connections from diagnostic tools but MUST treat GenWatch's connection as the authoritative one. | |
| Keepalive | TCP keepalive RECOMMENDED on both ends | `KEEPIDLE=30, KEEPINTVL=10, KEEPCNT=3` (matches GenWatch's existing client) |
| Idle behaviour | The ATS-Pi MUST NOT close an idle connection. GenWatch may legitimately go silent for 1.5 s between polls. | |

### 3.1 Endianness and word order

| Aspect | Convention |
|---|---|
| 16-bit word byte order on the wire | **Big-endian** (Modbus standard) |
| Multi-word value word order | **Big-endian** — the *high* word comes at the lower address |
| Example: u32 value `0x12345678` at address `0x0010` | `0x0010` ← `0x1234`, `0x0011` ← `0x5678` |

This matches the H-100 convention used elsewhere in GenWatch (see `registers/h100.yaml` notes on the `u32` type).

### 3.2 Address space

Modbus PDU addresses, hex. Reads and writes share the same address space (a write to `0x0100` is reflected in the read at `0x0040`-ish — see §6 for the exact mapping).

| Range | Purpose |
|---|---|
| `0x0000 – 0x000F` | Core state (most frequently polled) |
| `0x0010 – 0x001F` | Timestamps |
| `0x0020 – 0x002F` | Lifetime counters / statistics |
| `0x0030 – 0x003F` | Identification and contract version |
| `0x0040 – 0x004F` | Command read-back (echo of current driven outputs) |
| `0x0100 – 0x010F` | Command write registers |
| `0x0050 – 0x00FF` | RESERVED — must read as 0, MUST NOT be written |
| `0x0110 – 0xFFFF` | RESERVED |

---

## 4. Polling and timing

| Aspect | Value |
|---|---|
| GenWatch's prime poll cadence | **1.5 s** (configurable; this is the default) |
| GenWatch's base poll cadence | **15 s** (configurable) |
| ATS-Pi's internal sampling rate | **≥ 10 Hz** RECOMMENDED for the contact inputs, with hardware debounce of ≥ 5 ms |
| Maximum permitted response latency | **800 ms** per read/write at the ATS-Pi (well under GenWatch's 1.5 s timeout) |
| Per-read timeout (GenWatch side) | 1.0 s |
| Reconnect backoff (GenWatch side) | 0.25, 0.5, 1.0 s on consecutive failures |

GenWatch will poll the **core state block** (`0x0000-0x000F`) on the prime tier (every 1.5 s) and the **stats / timestamps / identification** (`0x0010-0x003F`) on the base tier (every 15 s). The ATS-Pi MUST handle the prime cadence indefinitely without performance degradation.

---

## 5. Read register map

All registers listed here MUST be implemented. Reading a RESERVED address MUST return `0x0000` (not an exception).

### 5.1 Core state (`0x0000 – 0x000F`)

These are the high-priority signals — GenWatch polls them every 1.5 s.

| Addr | Words | Type | Field | Encoding | Notes |
|---|---|---|---|---|---|
| `0x0000` | 1 | u16 enum | `position` | `0`=utility, `1`=generator, `2`=transferring, `3`=unknown | "transferring" is asserted while the ATS-Pi sees the load-disconnect pulse (≤ 2 s typically). "unknown" only on the first few hundred ms after ATS-Pi boot, before contacts are sampled. |
| `0x0001` | 1 | u16 bool | `normal_available` | `0`=not available, `1`=available | From 18RX RL6 N/O contact. |
| `0x0002` | 1 | u16 bool | `emergency_available` | `0`=not available, `1`=available | From 18RX RL5 N/O contact. |
| `0x0003` | 1 | u16 bool | `engine_start_calling` | `0`=relaxed, `1`=asserted | Sense (not drive) of the ATS engine-start output. `1` while the ATS is requesting a start from the H-100. |
| `0x0004` | 1 | u16 enum | `ats_mode` | `0`=auto, `1`=manual / locked-out, `2`=test, `3`=unknown | Optional — if the ATS-Pi can't determine this, return `3` and document the reason in `fault_summary`. |
| `0x0005` | 1 | u16 bitfield | `fault_summary` | See §5.1.1 | Self-reported ATS-Pi health bits. `0x0000` = nominal. |
| `0x0006` – `0x000F` | — | — | RESERVED | Read as `0x0000` | Future expansion. |

#### 5.1.1 `fault_summary` bit definitions

| Bit | Mask | Name | Meaning |
|---|---|---|---|
| 0 | `0x0001` | `INPUT_FAULT` | One or more ATS position/availability sense signals are stuck, failing, or unreadable — e.g. the hybrid Group 5 RS-485 link is down (the ATS-Pi is then "reachable but blind", still serving its last-good position), or both position signals read low. GenWatch treats this as position-untrustworthy and drops the ATS-Pi as authoritative (§10). |
| 1 | `0x0002` | `OUTPUT_FAULT` | A driven relay failed the read-back check (commanded high but reading low) |
| 2 | `0x0004` | `MODE_UNKNOWN` | `ats_mode` cannot be determined |
| 3 | `0x0008` | `CALIBRATION` | Impossible position state — both position-sense signals read asserted at once. A physically impossible combination: a welded/miswired aux contact or a bad Group 5 status bit. Not a transient — requires operator review of the Group 5 status map / aux contacts. Treated like INPUT_FAULT: position-untrustworthy, ATS-Pi dropped as authoritative (§10). |
| 4-15 | — | RESERVED | Must be 0 |

GenWatch will surface any non-zero `fault_summary` as a warn-severity alarm with code `ATS_PI_FAULT` and a meta string decoding the active bits.

### 5.2 Timestamps (`0x0010 – 0x001F`)

All timestamps are **Unix epoch seconds (UTC), u32 big-endian**.

| Addr | Words | Type | Field | Encoding | Notes |
|---|---|---|---|---|---|
| `0x0010` | 2 | u32 | `last_transfer_to_gen_ts` | epoch s | When the ATS last transferred load *to* the generator. `0` if never observed since the ATS-Pi was installed. |
| `0x0012` | 2 | u32 | `last_retransfer_to_util_ts` | epoch s | When the ATS last retransferred *to* utility. `0` if never observed. |
| `0x0014` | 2 | u32 | `ats_pi_uptime_s` | seconds since boot | Monotonic, resets only on reboot. Lets GenWatch detect an undetected reboot of the ATS-Pi. |
| `0x0016` | 2 | u32 | `ats_pi_wallclock` | epoch s | The ATS-Pi's *current* wall-clock time at the moment of read. GenWatch will compare against its own clock; a discrepancy > 5 s raises a `TIME_SKEW` warn. |
| `0x0018` – `0x001F` | — | — | RESERVED | | |

### 5.3 Lifetime counters / statistics (`0x0020 – 0x002F`)

| Addr | Words | Type | Field | Encoding | Notes |
|---|---|---|---|---|---|
| `0x0020` | 2 | u32 | `transfer_count_lifetime` | count | Total transfers to generator the ATS-Pi has observed. Persists across reboots (stored to disk). |
| `0x0022` | 2 | u32 | `transfer_count_24h` | count | Rolling 24-hour window. Resets every UTC midnight is acceptable; rolling window is preferred. |
| `0x0024` – `0x002F` | — | — | RESERVED | | |

### 5.4 Identification and contract version (`0x0030 – 0x003F`)

| Addr | Words | Type | Field | Encoding | Notes |
|---|---|---|---|---|---|
| `0x0030` | 1 | u16 | `icd_version_major` | unsigned int | This document's version (current: `1`). On a `major` mismatch GenWatch drops the ATS-Pi as authoritative — loadSource falls back to the H-100 derivation (§10) and a warn-severity event is raised — but the poller keeps running, so the ATS block still appears in the UI flagged non-authoritative. |
| `0x0031` | 1 | u16 | `icd_version_minor` | unsigned int | Minor version (current: `0`). A minor mismatch never blocks operation — only a **major** mismatch drops the ATS-Pi as authoritative (§10). Minor-ahead logs an info note (the companion may expose registers this GenWatch build doesn't read); minor-behind logs an error and raises a warn-severity event (registers GenWatch expects read as `0`), but GenWatch keeps operating on the fields it can read. |
| `0x0032` | 1 | u16 | `ats_pi_fw_major` | unsigned int | ATS-Pi software major version. Diagnostic only. |
| `0x0033` | 1 | u16 | `ats_pi_fw_minor` | unsigned int | |
| `0x0034` | 1 | u16 | `ats_pi_fw_patch` | unsigned int | |
| `0x0035` | 1 | u16 | `ats_pi_unit_id` | unsigned int | Site-specific identifier so GenWatch can refuse to talk to a wrong-site ATS-Pi if someone misconfigures. SITE-23 = `23`. |
| `0x0036` – `0x003F` | — | — | RESERVED | | |

### 5.5 Command read-back (`0x0040 – 0x004F`)

Each command register at `0x0100+N` has a corresponding read-back at `0x0040+N`. The read-back reflects the *currently-driven* output state, not the most-recent-written value (in case the ATS-Pi enforces safety interlocks that ignore a write — see §8).

| Addr | Words | Type | Field | Notes |
|---|---|---|---|---|
| `0x0040` | 1 | u16 bool | `cmd_test_active` | `1` while the test contact is currently being pulsed. Goes back to `0` after the pulse completes. |
| `0x0041` | 1 | u16 bool | `cmd_inhibit_active` | `1` while inhibit is asserted (maintained). |
| `0x0042` | 1 | u16 bool | `cmd_force_transfer_active` | `1` while force-transfer is asserted (maintained). |
| `0x0043` | 1 | u16 bool | `cmd_bypass_delay_active` | `1` while bypass-delay is being asserted. |
| `0x0044` – `0x004F` | — | — | RESERVED | |

---

## 6. Write register map

GenWatch writes to these registers to drive the ATS commands. All writes are FC06 (single register) with the values defined below. The ATS-Pi MUST validate the value and reject any write that doesn't match a defined pattern by returning a Modbus exception. **GenWatch treats *any* Modbus exception (or write failure) as "command rejected" and does not inspect the exception code** — so the specific codes called out below are advisory conventions (useful for diagnostic tools), not a distinction GenWatch enforces.

### 6.1 Command registers (`0x0100 – 0x010F`)

| Addr | Field | Pattern | Behaviour | Permitted in `ats_mode` |
|---|---|---|---|---|
| `0x0100` | `cmd_test` | Write `0x0001` | ATS-Pi pulses the ASCO test input (terminals 6-7) for **≥ 500 ms, ≤ 1500 ms**, then releases. Idempotent: writes while `cmd_test_active=1` are ignored. | `auto` only |
| `0x0101` | `cmd_inhibit` | Write `0x0001` to assert, `0x0000` to release | Maintained signal on the ASCO inhibit input (terminals 10-11). Stays asserted until released or until the ATS-Pi loses comms with GenWatch for > 30 s (safety auto-release — see §8.3). | `auto`, `manual` |
| `0x0102` | `cmd_force_transfer` | Write `0x0001` to assert, `0x0000` to release | Maintained signal on the ASCO force-transfer-to-emergency input (terminals 8-9). Same comms-timeout auto-release as inhibit. **High consequence — admin-only on the GenWatch side.** | `auto` only |
| `0x0103` | `cmd_bypass_delay` | Write `0x0001` | Pulses the bypass-delay input (terminals 12-13) for **≥ 500 ms, ≤ 1500 ms**. | `auto` only |
| `0x0104` – `0x010F` | — | — | RESERVED — reject writes with a Modbus exception (`0x03` illegal data value by convention) | — |

### 6.2 Write semantics

- A write to any command register MUST return success (not an exception) within 100 ms of receipt, even if the actual relay actuation takes longer.
- The driven output state MUST be reflected in the corresponding read-back register (`0x0040+N`) within 500 ms.
- If the ATS-Pi cannot honour a write because of a permitted-mode restriction (see "Permitted in" column), it MUST reject the write with a Modbus exception (`0x04` server device failure by convention) and surface `fault_summary` bit `INPUT_FAULT` until the next valid command attempt clears it. As above, GenWatch does not distinguish exception codes — any exception surfaces to the operator as a rejected command (HTTP 502).

---

## 7. Encoding conventions

| Type | Width | Notes |
|---|---|---|
| `u16` | 1 word | Big-endian byte order on the wire (Modbus standard) |
| `u32` | 2 words | High word at lower address |
| `bool` | 1 word | `0x0000` = false, `0x0001` = true. Other non-zero values MUST be treated as true by GenWatch but the ATS-Pi MUST emit `0x0001` for true. |
| `enum` | 1 word | Document the value set per-field. Unknown values are surfaced as `unknown` in the UI. |
| `bitfield` | 1 word | Each bit independently meaningful. Document mask per bit. |
| `epoch s` | u32 | Unix seconds, UTC. `0` is the "never" sentinel — MUST NOT be a real timestamp (which the year-1970 limitation makes a non-issue in practice). |

**Scale factors:** *Not used in v1.* All values in this ICD are dimensionless integers or already in their natural unit (seconds, counts, booleans). Future versions adding metering will define scale factors per-register.

**Sign:** *No signed values in v1.* If metering adds signed power values (negative kW = export), it MUST use `s32` with explicit two's-complement.

---

## 8. Semantic contracts

These are guarantees that hold *regardless* of the wire-level details — they describe the meaning the ATS-Pi must uphold.

### 8.1 Atomicity

The ATS-Pi MUST sample all four contact inputs (utility-avail, emergency-avail, position-normal, position-emergency) **within a single coherent sampling window** before publishing the values to the read registers. GenWatch must never see, e.g., `position=utility` AND `normal_available=0` AND `engine_start_calling=0` simultaneously — that combination is physically impossible and represents a torn read.

Implementation suggestion (non-binding): single-threaded poll loop that snapshots all inputs at the top of each cycle, then atomically swaps the published register block.

### 8.2 Monotonicity

- `transfer_count_lifetime` MUST be monotonically non-decreasing across reads. It MAY increment between two reads.
- `ats_pi_uptime_s` MUST be monotonically increasing within a single ATS-Pi boot. A backwards jump indicates a reboot or wall-clock NTP correction; GenWatch detects this via `uptime_s` going backwards or `wallclock` jumping > 5 s.
- Timestamp fields (`last_transfer_to_gen_ts`, etc.) MAY only be updated to a value ≥ the previous value, with one exception: an ATS-Pi reset that wipes persistent storage MAY result in `0` (never observed) reappearing. GenWatch handles this by treating `0` as "missing data, do not display."

### 8.3 Comms-loss safety auto-release

This is a critical safety contract. **If the ATS-Pi has not received a successful Modbus read from GenWatch for `30 ± 5 s`, it MUST automatically release any maintained command outputs (inhibit, force-transfer)** by treating them as if GenWatch had written `0x0000`. The read-back registers MUST reflect this auto-release within the standard 500 ms.

Rationale: an operator who issued `force_transfer=1` and then lost the GenWatch UI (network failure, browser crash, etc.) must not leave the ATS commanded into a manual state with no way to release it remotely. The 30 s timeout gives GenWatch plenty of margin to recover comms while ensuring the ATS returns to a safe state if recovery doesn't happen.

The pulsed commands (`test`, `bypass_delay`) are unaffected — they self-clear within ≤ 1.5 s regardless of comms state.

### 8.4 Engine-start sensing is read-only

`engine_start_calling` (`0x0003`) reflects whether the ATS is *currently* asserting its engine-start output to the H-100. The ATS-Pi MUST NOT drive this signal. It is a pure sense input, used by GenWatch to cross-correlate ATS-initiated starts vs operator-initiated starts.

### 8.5 No silent state changes

Every observable state change (position, source-availability, mode, fault) MUST be reflected in the next polled read after the underlying contact stabilizes (≤ 100 ms after debounce). The ATS-Pi MUST NOT batch, delay, or coalesce state changes.

---

## 9. Failure handling

### 9.1 ATS-Pi unreachable from GenWatch

| GenWatch behaviour |
|---|
| `loadSource` derivation falls back to the existing H-100-electrical method (see `services/state.py:_derive_load_source`). The UI ATS card grays out with a "ATS link lost" badge. |
| Comms loss after `> 3 × prime_poll_ms` (default 4.5 s) raises a `comms` event of severity `warn` on the ATS link, distinct from the H-100 link's `comms` event. |
| All ATS command buttons in the UI disable until the link recovers. |
| Comms recovery raises an `ok`-severity comms event. |
| If the ATS-Pi was unreachable for > 30 s, GenWatch assumes any commands it had asserted have auto-released per §8.3. It MUST NOT silently re-assert them: the plant state may have changed since the operator's original intent, so an automatic re-assert of force-transfer or inhibit is more dangerous than leaving the ATS on its own automatic logic. Instead, GenWatch emits a warn-severity event when the read-back confirms the release; the operator re-issues the command deliberately if it is still wanted. |

### 9.2 GenWatch unreachable from ATS-Pi

| ATS-Pi behaviour |
|---|
| Continues sampling and updating its register block normally. |
| After 30 s of no successful read from GenWatch, executes the §8.3 safety auto-release on all maintained commands. |
| Reverts to whatever default state the ATS would be in without ATS-Pi commands (which is: ATS operates on its own automatic logic, exactly as it does today). |
| Logs the comms loss to its own diagnostic log (out of scope for this ICD). |

### 9.3 ATS-Pi reboot

| Behaviour |
|---|
| The ATS-Pi MUST come up in a "no commands asserted" state. All write registers start at `0x0000`, all read-back registers read `0x0000`. |
| `transfer_count_lifetime` MUST be persistent — restoration of this counter from disk is REQUIRED. Other registers MAY initialize to default values. |
| `last_transfer_to_gen_ts` / `last_retransfer_to_util_ts` SHOULD be persistent but MAY reset to `0` if persistence is not implemented. |
| `ats_pi_uptime_s` resets to `0` and counts from boot. |
| GenWatch detects an undetected reboot by observing `uptime_s` going backwards and logs an `ATS_PI_REBOOT` info-severity event. |

### 9.4 Time skew

If GenWatch's wall-clock and the ATS-Pi's `ats_pi_wallclock` differ by > 5 s, GenWatch raises a `TIME_SKEW` warn-severity alarm. Both sides should run NTP; in a single-site LAN that requires no special configuration. The alarm clears automatically once the skew drops back under 5 s.

---

## 10. Source-of-truth precedence

When the ATS-Pi link is healthy, the ATS-Pi's `position` field is the authoritative `loadSource` value displayed to the operator. When the link is degraded or lost, GenWatch's existing H-100-derived `loadSource` (utility/generator inferred from engine state + output kW + current) is the fallback.

The fallback derivation is documented in `backend/genwatch/services/state.py:_derive_load_source` and tested in `backend/tests/test_state_machine.py`. It remains the operator-visible value while ATS-Pi comms are anything other than `healthy`.

The UI MUST annotate the `loadSource` value with its provenance:
- Healthy ATS-Pi → no annotation (this is the assumed normal case)
- H-100 fallback → small "(via gen telemetry)" subscript

This precedence is intentionally one-way: GenWatch does not "vote" between the two sources or take a majority. The ATS-Pi reads the switch's actual position contacts; the H-100 derivation is an inference. The direct measurement wins whenever it's available.

**Exception — position-untrustworthy faults.** A healthy Modbus TCP link does *not* by itself mean the served `position` is fresh. If the ATS-Pi reports `INPUT_FAULT` or `CALIBRATION` (§5.1.1) — most importantly the hybrid case where the Group 5 RS-485 sense link drops while the Modbus TCP link to GenWatch stays up, so the ATS-Pi keeps serving a frozen last-good position — GenWatch treats the ATS-Pi as **non-authoritative** even though comms are `healthy`, falls back to the H-100 derivation (with the "(via gen telemetry)" provenance), and refuses operator command *asserts* until the fault clears. Command *releases* (the fail-safe direction) remain permitted.

---

## 11. Time synchronization

Both Pis MUST run NTP (or chronyd) against the same time source. For a single-site LAN with no upstream NTP server, the site's router typically serves time; if not, one of the Pis SHOULD be configured as the NTP master.

Acceptable skew between the two Pis: **< 5 seconds**. The `TIME_SKEW` alarm fires above this threshold.

---

## 12. ICD versioning

| Aspect | Rule |
|---|---|
| Major version bumps | Backwards-incompatible changes (renumbering registers, changing encodings, removing fields). Both sides must be updated in lockstep. |
| Minor version bumps | Backwards-compatible additions (new registers in RESERVED space, new bits in existing bitfields where the unset value is the safe default). Older consumers must continue to work against newer producers. |
| Field deprecations | A deprecated field continues to be served (with documented "deprecated as of v1.X" status) for at least one minor version cycle before removal in the next major. |

A version mismatch is detectable at startup via the `icd_version_major`/`icd_version_minor` registers (§5.4). GenWatch logs the observed version on every successful connect.

---

## 13. Test fixtures

To validate the contract end-to-end without real ATS hardware, both projects SHOULD implement a mock counterpart:

- **ATS-Pi mock (for GenWatch dev):** a single Python script that serves the ICD's register layout with hand-driven test states. GenWatch's existing `MockModbusClient` pattern (see `backend/genwatch/modbus/client.py`) is the model. Run it on the dev Pi or a laptop; GenWatch points at it via `ats.host`.
- **GenWatch mock (for ATS-Pi dev):** a stub Modbus TCP client that polls the ATS-Pi's exposed registers and reports the wire behaviour. The ATS-Pi team can use `modpoll` for simple reads; for write-then-read-back cycles, a small Python script is sufficient.

A shared **golden test sequence** is RECOMMENDED:

| t (s) | Action | Expected ATS-Pi state |
|---|---|---|
| 0 | Boot ATS-Pi; both sources healthy; load on Normal | `position=0, normal_avail=1, emergency_avail=1, engine_start_calling=0` |
| 30 | Open utility breaker | `position=0` (still — gen not started yet), `normal_avail=0`, `engine_start_calling=1` |
| 35 | (Wait for H-100 to start engine; ATS transfers) | `position=2` (transferring) for ~1 s, then `position=1`, `last_transfer_to_gen_ts=<now>` |
| 600 | Close utility breaker | `normal_avail=1`. After ATS retransfer delay: `position=2`, then `position=0`, `last_retransfer_to_util_ts=<now>`. `engine_start_calling` goes to `0` after retransfer. |

Both projects MUST run this sequence (with mocks) before each ICD-affecting release.

---

## 14. Glossary

| Term | Definition |
|---|---|
| **ATS-Pi** | The companion Raspberry Pi running the ATS-monitoring project. Sole physical interface to the ASCO. |
| **GenWatch** | The existing generator-monitoring project on its own Pi. Consumes the ATS-Pi's exposed registers. |
| **ASCO** | ASCO Series 300 Power Transfer Switch, P/N J00300030600N1X0 on this site. |
| **18RX** | ASCO Relay Expansion Module providing source-availability contacts RL5 (emergency) and RL6 (normal). |
| **14AA / 14BA** | ASCO auxiliary contact kits on the switch mechanism providing position-Normal and position-Emergency contacts. |
| **Modbus TCP** | Modbus protocol over TCP with the MBAP header — *not* Modbus RTU encapsulated in TCP. |
| **Prime poll** | GenWatch's fast (1.5 s default) polling tier for fast-changing state. |
| **Base poll** | GenWatch's slow (15 s default) polling tier for telemetry and counters. |
| **loadSource** | GenWatch's derived "where is the load right now" value, one of `utility` / `generator` / `unknown`. |

---

## Appendix A. ASCO Series 300 terminal reference (this site)

ATS-Pi wiring reference. In the deployed **hybrid** driver the *read*
("DI (read)") rows below are sensed from the Group 5 controller over
RS-485 serial rather than wired as discrete contacts; the *drive*
("DO (drive)") rows are the ADAM-6060 command-relay landings and are
wired as shown. The full wiring/BOM lives in the companion
[`ats-pi-companion`](https://github.com/SethMorrowSoftware/ats-pi-companion)
repo.

| Terminal | Function | Type | Rating | Wired to ATS-Pi as |
|---|---|---|---|---|
| 1, 2, 3 | Load Disconnect Contacts | Output (Form C dry) | 120 VAC / 5 A | DI (read) — drives `position` transitioning state |
| 4, 5 | Engine Exerciser | Input | 5 VDC / 5 mA | not used (H-100 owns engine exerciser) |
| 6, 7 | Momentary Test Switch | Input | 5 VDC / 5 mA | DO (drive) — implements `cmd_test` |
| 8, 9 | Maintained Transfer to Emergency | Input | 5 VDC / 5 mA | DO (drive) — implements `cmd_force_transfer` |
| 10, 11 | Inhibit Transfer to Emergency | Input | 5 VDC / 5 mA | DO (drive) — implements `cmd_inhibit` |
| 12, 13 | Bypass Transfer Time Delay | Input | 5 VDC / 5 mA | DO (drive) — implements `cmd_bypass_delay` |
| 14, 15, 16 | Factory use — **do not wire** | — | — | not used |

Plus, on the switch mechanism / sub-bracket:

| Accessory | Contact | ATS-Pi mapping |
|---|---|---|
| 14AA aux contact | N/O | DI → drives `position` (normal side) |
| 14BA aux contact | N/O | DI → drives `position` (emergency side) |
| 18RX RL5 | Form C | DI → `emergency_available` |
| 18RX RL6 | Form C | DI → `normal_available` |
| Engine-start TB (separate) | Form A | DI → `engine_start_calling` (sense only — H-100 wire stays in place) |

---

## Appendix B. Example wire trace

A normal utility-outage cycle, as it should appear at GenWatch on the prime-poll feed:

```
t=0.0    R 0x0000-0x0005: position=0 norm=1 emerg=1 startcall=0 mode=0 fault=0
t=1.5    R 0x0000-0x0005: position=0 norm=1 emerg=1 startcall=0 mode=0 fault=0
t=3.0    R 0x0000-0x0005: position=0 norm=0 emerg=1 startcall=1 mode=0 fault=0   ← utility loss + ATS calls H-100
t=4.5    R 0x0000-0x0005: position=0 norm=0 emerg=1 startcall=1 mode=0 fault=0
... (engine cranks, ~6 s to running, ATS waits for gen-ready threshold)
t=15.0   R 0x0000-0x0005: position=2 norm=0 emerg=1 startcall=1 mode=0 fault=0   ← transferring
t=16.5   R 0x0000-0x0005: position=1 norm=0 emerg=1 startcall=1 mode=0 fault=0   ← settled on gen
         R 0x0010-0x0013: last_transfer_to_gen_ts=<wallclock at t=15.0>
... (carrying load)
t=600.0  R 0x0000-0x0005: position=1 norm=1 emerg=1 startcall=1 mode=0 fault=0   ← utility restored
... (ATS retransfer time delay, typically 5-30 min — ATS-side setting)
t=900.0  R 0x0000-0x0005: position=2 norm=1 emerg=1 startcall=0 mode=0 fault=0   ← retransferring
t=901.5  R 0x0000-0x0005: position=0 norm=1 emerg=1 startcall=0 mode=0 fault=0   ← back on utility
         R 0x0010-0x0017: last_retransfer_to_util_ts=<wallclock at t=900.0>
... (H-100 enters cool-down on its own — ATS no longer commanding start)
```

GenWatch derives the following events from these reads:
- `t=3.0`: `UTILITY_LOST` (warn) — `normal_available` 1→0
- `t=15.0`: `LOAD_SOURCE` (warn) — load transitioning to GENERATOR
- `t=16.5`: settled on gen
- `t=600.0`: `UTILITY_RESTORED` (ok)
- `t=900.0`: load transitioning back
- `t=901.5`: `LOAD_SOURCE` (ok) — load back on UTILITY

These appear in GenWatch's unified events feed alongside the H-100's `TRANSITION` (engine-state) events. Operators see one timeline.

---

*End of ICD v1.0.*
