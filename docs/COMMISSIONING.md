# Commissioning Runbook — Castle Generator Monitor + ATS-Pi

**Field procedure for bringing the system live against real hardware.**
The software is complete and tested against mocks; this runbook is the
hardware-in-the-loop validation that must pass before the system is
trusted to monitor — and command — a real generator and transfer switch.

> **Read this first.** Work the phases **in order** and do not skip a
> STOP gate. Each phase proves the layer beneath it; commands (which
> physically actuate the generator/ATS) come **last**, only after the
> read side is proven and during a planned outage window. A wrong
> register map or a miswired contact is the failure mode this runbook
> exists to catch *before* it can act on the plant.

| | |
|---|---|
| **System** | Generac H-100 (via Lantronix bridge) + ASCO Series 300 (via the hybrid ATS-Pi — Group-5 serial sense + ADAM-6060 command relays) |
| **Audience** | Commissioning engineer + a qualified electrician for any in-cabinet work |
| **Companion docs** | `integrations/ats-pi-icd.md` · `integrations/ats-pi-plan.md §8` · the [`ats-pi-companion`](https://github.com/SethMorrowSoftware/ats-pi-companion) repo (`HARDWARE.md`, RS-485 bench-verify, `SPEC.md`) |
| **Estimated time** | ~½ day for H-100 (Phases 1-4); ATS adds ~1 day incl. wiring + an outage window |

---

## 0. Safety, roles, and prerequisites

### 0.1 Safety

- **All in-cabinet ATS/generator work is 480 V / 600 A.** It requires a
  qualified electrician with PPE and **LOTO per NFPA 70E**. The
  commissioning engineer does the software/Modbus steps; the electrician
  does the wiring steps (Phase 6).
- **Phases 1-3 and 5-7 are non-actuating** (read-only / bench). **Phases
  4 and 8 physically start/stop the generator and transfer load** —
  schedule them, brief the site, and have someone stationed at the unit
  who can hit the local **E-stop** and the panel key switch.
- The H-100 front-panel **key switch is your master interlock**: in
  MANUAL/OFF the controller ignores every remote command. Keep it
  **OFF or MANUAL** until you intend to test remote control (Phase 4).

### 0.2 Roles

| Role | Responsible for |
|---|---|
| Commissioning engineer | Pi install, Modbus verification, register-map adjustment, sign-off |
| Qualified electrician | LOTO, in-cabinet wiring (Phase 6), re-energization |
| Site operator | Witnesses Phases 4 & 8, trained on the new controls at the end |

### 0.3 Tools / prerequisites

- [ ] Laptop on the same LAN; SSH access to the GenWatch Pi (and ATS-Pi, if used)
- [ ] `modpoll` or `mbpoll` installed on the laptop/Pi for raw Modbus reads
- [ ] The **H-100 front panel visible** (or a second person reading it) for the value cross-check
- [ ] The ASCO operator's manual (`381333-289`) for *this unit's* terminal numbering
- [ ] Bridge IP/port confirmed (default Lantronix `192.168.1.249:10001`)
- [ ] A planned outage window booked for Phases 4 and 8

### 0.4 Conventions

- `PASS` lines are the go/no-go criteria. **If a PASS criterion fails, do
  not proceed — drop to the "If it fails" notes and resolve it first.**
- Commands run on the **GenWatch Pi** unless prefixed `[ATS-Pi]` or `[laptop]`.

---

## Phase 1 — Network & power pre-checks (non-actuating)

**Goal:** confirm the boxes are powered, reachable, and on protected power.

1. Bridge/ATS power audit — the Lantronix bridge **and** any switch
   between the Pi and the bridge must be on the **generator's load side
   or a UPS**, or a utility outage blinds you exactly when you need
   telemetry most.
2. From the Pi:
   ```bash
   ping -c 3 192.168.1.249          # bridge
   nc -vz 192.168.1.249 10001       # expect "succeeded"
   ```
3. NTP: `chronyc tracking` (or `timedatectl`) — clock disciplined.

- **PASS:** bridge pings, `nc` succeeds, clock synced.
- **If it fails:** README §3 / §10 (bridge listener, VLAN ACLs, Pack Control).

☐ Phase 1 complete — signed ______________ date __________

---

## Phase 2 — GenWatch install + H-100 link (non-actuating)

**Goal:** the service runs, authenticates, and the Modbus link to the
H-100 answers.

1. Install:
   ```bash
   git clone https://github.com/SethMorrowSoftware/GenWatch.git ~/GenWatch
   cd ~/GenWatch && sudo ./deploy/scripts/install.sh
   ```
   - Watch for `Hardware watchdog active (...)` near the end. If it says
     **NOT active**, note it — reboot applies it (the install asserts this now).
2. Set the admin password + point at the bridge:
   ```bash
   sudo genwatch hash                       # paste the $2b$... hash into config
   sudo nano /etc/genwatch/config.yaml      # admin_password_hash + modbus_tcp.host
   sudo systemctl restart genwatch
   ```
   - The service **refuses to boot** if `jwt_secret`/`admin_password_hash`
     are unset or the `REPLACE_ME` placeholder — that's intended. The
     installer sets `jwt_secret`; you set the password hash.
3. Pre-flight + live probe:
   ```bash
   sudo genwatch doctor
   ```

- **PASS:** `doctor` reports `Modbus: slave 100 responded ...`, and
  `systemctl status genwatch` is `active (running)`.
- **If it fails:** `Modbus: NO RESPONSE` on an open socket is almost
  always the bridge's **Pack Control / Idle Gap** splitting RTU frames
  (README §10) — set idle gap ~10 ms, 9600 8N1. Wrong slave ID → check
  `modbus.slave: 100`. Won't boot → `journalctl -u genwatch -e`.

☐ Phase 2 complete — signed ______________ date __________

---

## Phase 3 — H-100 register-map verification (READ-ONLY) ⚠ critical

**Goal:** prove every value GenWatch decodes matches the H-100's own
LCD. **This is the most important read-only step** — the shipped map is
the genmon community map and your panel may be a G-Panel, a
dealer-customized firmware, or use different scales. The start/stop bit
patterns ride on this map too.

1. Generate the cross-check sheet and take it to the panel:
   ```bash
   sudo -u genwatch genwatch panel --html > /tmp/crosscheck.html
   # open in a browser → Print, or:
   sudo -u genwatch genwatch panel          # text version
   ```
2. Standing at the H-100, compare **each** value: engine state, frequency,
   voltages (L-L), currents, oil pressure/temp, coolant temp, battery,
   fuel %, run hours. Values flagged with `←` by `genwatch panel` are
   structurally suspicious — scrutinize them.
3. Spot-check the raw status block and a telemetry register:
   ```bash
   sudo -u genwatch genwatch modbusdump --addr 0x0080 --count 16   # state/alarm bitfields
   sudo -u genwatch genwatch modbusdump --addr 0x00B2 --count 2    # frequency (raw 600 = 60.0 Hz)
   ```
4. Open the UI (`http://<pi>:8000`), log in, and confirm the **Live view
   populates within ~2 s**, the **Comms badge is green**, and no false
   **STALE DATA** badge.

- **PASS:** every panel value matches the UI / `genwatch panel` (within
  rounding), engine state is correct, and the **PANEL chip** in the
  top-right matches the physical key-switch position when you toggle it.
- **If it fails:**
  - Values garbage but link healthy → likely a **G-Panel** (addresses
    shift) or dealer firmware. Use `genwatch scan --start 0x0000 --end
    0x07FF` to locate, then edit `/opt/genwatch/genwatch/registers/h100.yaml`
    (README §12), `curl -b cookies .../api/registers/verify` then
    `.../api/registers/reload` — no restart needed.
  - PANEL chip stuck on `?` while the switch is in AUTO → your
    `panel_mode_bits` differ from genmon defaults. AUTO (`0x8000`) is
    firmly known; **MANUAL/OFF are best-guess defaults** — fix them now
    (see Phase 4.0) while you're at the panel.
  - One value 10×/100× off → a `scale` mismatch in the YAML.

> **STOP GATE.** Do not proceed to Phase 4 until **every** value matches
> and the engine state + PANEL chip are correct. Remote control trusts
> this map.

☐ Phase 3 complete — every value cross-checked — signed ______________ date __________

---

## Phase 4 — H-100 control verification (ACTUATING — planned, operator present)

**Goal:** prove remote start/stop/exercise behave, and the safety gates
fire. **The generator will run.** Operator stationed at the unit.

### 4.0 Verify the panel-mode gate (before any command)

1. With the key switch in **MANUAL or OFF**, open the UI. The control
   buttons must be **disabled** and the PANEL chip must show MANUAL/OFF.
2. Toggle to **AUTO**; within ~1.5 s the chip flips to AUTO and the
   buttons enable. If the chip is wrong for any position, fix
   `panel_mode_bits` (Phase 3 notes) before continuing.

- **PASS:** buttons are gated correctly by the physical key switch.

### 4.1 Remote start / stop

1. Key switch in **AUTO**. From the UI, **Remote Start** → confirm the
   two-step modal (8-char token... now a longer hex code, 30 s TTL).
2. Engine should crank within ~2 s. Watch state go `cranking → running`.
3. **Remote Stop** → cool-down → stopped.

- **PASS:** start/stop work; the Events feed + audit log record each
  command with operator, register, and result.

### 4.2 Negative tests (the gates must *block*)

- With the engine **running**, click **Start** → expect `409 invalid_state`
  (button should be disabled anyway).
- Turn the key switch to **MANUAL**, click **Stop** (force via the
  disabled-state if needed) → expect `409 panel_mode_locked`.
- Pull the bridge network cable briefly → Comms goes **LOST**, the STALE
  badge appears, and controls are **blocked** (`409 comms_lost`). Reconnect.

- **PASS:** all three gates reject as described and are audit-logged.

### 4.3 Alarm-ack (optional, if a non-shutdown alarm is present/inducible)

- Acknowledge an active alarm → confirm the UI reports success **only if**
  the controller write succeeded (no false "Acknowledged"), and the panel
  light clears.

☐ Phase 4 complete — controls + gates verified — signed ______________ date __________

---

> ### H-100-only deployments stop here.
> If there is no ATS-Pi, the system is commissioned. Complete §9 sign-off
> and §8 Security (README §8). Phases 5-8 are ATS-Pi only.

---

## Phase 5 — ADAM-6060 bench verification (BEFORE wiring to the ASCO)

**Goal:** confirm the ADAM's Modbus map and that every relay actuates —
**on the bench, with nothing wired to the ASCO yet.** This is where you
confirm the one thing the software couldn't: the real ADAM register map.

1. Power the ADAM-6060 (24 VDC), set its static IP (ships at `10.0.0.1`
   — use Advantech's utility; recommend `192.168.1.251`), put it on the
   OT VLAN. From the ATS-Pi:
   ```bash
   [ATS-Pi] ping -c 3 192.168.1.251
   [ATS-Pi] modpoll -m tcp -a 1 -r 1 -c 1 192.168.1.251   # packed DI register
   ```
2. **Configure the ADAM's own protection** (while you're in the
   Advantech utility — full rationale in the companion's
   `docs/HARDWARE.md §3.1`): **power-on value OFF for DO 0–5**, and
   **host watchdog / fail-safe enabled, timeout ~60 s, FSV OFF for
   DO 0–5**. This is the only layer that can release a relay if the
   ATS-Pi itself dies — the §8.3 software release can't outlive its
   own process. Then verify the power-on half: assert a relay
   (`modpoll`, or the utility's test page), **pull the ADAM's 24 VDC
   and re-power it** — every relay must come back **open**.
3. **Confirm the read map (DI).** Short each DI input in turn (or use the
   ADAM's test inputs) and confirm the packed DI register bit changes per
   `HARDWARE.md §3` (DI0 load-disconnect … DI5 engine-start). If your
   firmware exposes DI/DO differently than `io_adam.py`'s constants
   (`ADDR_PACKED_DI=0x0000`, `ADDR_PACKED_DO=0x0001`, DO coils `0x0010+`),
   **edit those constants** — they're isolated at the top of the file
   for exactly this.
4. **Confirm the write map (DO) — relays only, nothing wired to the ASCO.**
   Bring up the companion against the ADAM:
   ```bash
   [ATS-Pi] sudo nano /etc/atspi/config.yaml     # io.driver: adam, io.adam.host: 192.168.1.251
   [ATS-Pi] atspi --config /etc/atspi/config.yaml
   ```
   From a laptop, drive each command at the **ATS-Pi's** ICD registers and
   **listen for the relay click** + watch the read-back. **Note the
   modpoll convention:** its `-r` reference is **1-based = ICD PDU address
   + 1** (so PDU `0x0101`=257 → `-r 258`; PDU `0x0041`=65 → `-r 66`). The
   ICD command + read-back registers are holding registers (`-t 4`):

   | ICD field | PDU addr | modpoll `-r` |
   |---|---|---|
   | core state (position…fault) | 0x0000-0x0005 | `-r 1 -c 6` |
   | cmd_test (write) / read-back | 0x0100 / 0x0040 | `-r 257` / `-r 65` |
   | cmd_force_transfer / read-back | 0x0102 / 0x0042 | `-r 259` / `-r 67` |
   | cmd_inhibit / read-back | 0x0101 / 0x0041 | `-r 258` / `-r 66` |
   | cmd_bypass_delay / read-back | 0x0103 / 0x0043 | `-r 260` / `-r 68` |

   ```bash
   [laptop] modpoll -m tcp -a 1 -r 1   -c 6 192.168.1.250      # ICD core state
   [laptop] modpoll -m tcp -a 1 -r 258 -t 4 192.168.1.250 1    # cmd_inhibit assert → DO2 clicks
   [laptop] modpoll -m tcp -a 1 -r 66  -c 1 192.168.1.250      # cmd_inhibit read-back → 1
   [laptop] modpoll -m tcp -a 1 -r 258 -t 4 192.168.1.250 0    # release → DO2 opens, read-back → 0
   ```
   Repeat for test (`-r 257`, pulses + self-clears), force-transfer
   (`-r 259`), and bypass-delay (`-r 260`, pulses). (If your tool is
   `mbpoll`, the `-r` reference convention is the same.)
5. **Verify the §8.3 safety auto-release on the real relay:** assert
   inhibit (`-r 258` = 1), confirm DO2 closed, then **kill the laptop's
   Modbus session and wait**. Within **30 ± 5 s** the relay must drop and
   the read-back (`-r 66`) return to 0 with no further action.
6. **Verify the §9.3 boot reset:** assert inhibit again, then restart
   the service mid-assert (`[ATS-Pi] sudo systemctl restart atspi`, or
   `kill -9` the process and let systemd restart it). On startup the
   service must drive the relay **open** on its own —
   `journalctl -u atspi` shows `output release requested: boot reset
   (ICD §9.3)` followed by `outputs released`. A relay that stays
   closed across a service restart fails this step.
7. **Verify the ADAM fail-safe (FSV) end-to-end:** assert inhibit, then
   **stop the service entirely** (`sudo systemctl stop atspi` — nothing
   is polling the ADAM now, which models a dead/powerless ATS-Pi). The
   **module itself** must drop the relay within the FSV timeout
   configured in step 2 (~60 s). Restart the service afterwards.

- **PASS:** every DI bit tracks its contact, every command clicks the
  correct relay and reflects in the read-back, pulses self-clear, the
  30-second auto-release fires on a real relay, relays come up open on
  ADAM power-up and on `atspi` restart, and the ADAM's own FSV drops a
  relay with the service stopped.
- **If it fails:** DI/DO mapping wrong → adjust `io_adam.py` constants or
  the HARDWARE §3 wiring plan. Auto-release didn't fire → check the
  `atspi` safety watchdog log; **do not wire to the ASCO until it does.**
  FSV didn't fire → re-check the host-watchdog settings in the Advantech
  utility (HARDWARE §3.1); **do not wire to the ASCO without it** — it is
  the only release path that survives an ATS-Pi power loss.

> **STOP GATE.** Do not wire the ADAM to the ASCO until every relay is
> proven on the bench and ALL THREE release paths work: the 30 s §8.3
> auto-release, the §9.3 boot reset, and the module-level FSV.

☐ Phase 5 complete — ADAM map + relays + all three release paths verified on the bench — signed ______________ date __________

---

## Phase 6 — ASCO field wiring (ELECTRICIAN · LOTO)

**Goal:** land the command-relay field wires and the Group 5 serial link.
Per the companion `HARDWARE.md §3 + §5` and the RS-485 bench-verify guide.

1. **LOTO the ATS (utility AND generator sources).**
2. **Sensing (hybrid):** connect the USB-RS485 adapter to the Group 5
   controller's RS-485 port and confirm the controller's RS485 address +
   baud (front panel: General → Communication). No 18RX/14AA aux contacts
   are required — the ATS-Pi reads position, source availability, and
   engine-start from the Group 5 over serial.
3. **Commands:** land the DO wires (ADAM relays → ASCO input terminals
   6-7 / 8-9 / 10-11 / 12-13) per the companion HARDWARE §3. The ADAM DIs
   are unused in the hybrid driver. **Verify the terminal numbers against
   this unit's manual `381333-289`** — numbering varies by catalog number.
   **Never wire DO4/DO5** (terminals 14-16 are factory-use).
4. **Do not disturb** the existing engine-start wire to the H-100; the
   ATS-Pi only *senses* engine-start (from the Group 5 over serial).
5. Remove LOTO, re-energize.

- **PASS:** wiring lands on verified terminals, engine-start wire
  undisturbed, electrician signs off.

☐ Phase 6 complete — signed (electrician) ______________ date __________

---

## Phase 7 — ATS-Pi read-only verification (non-actuating)

**Goal:** with the ATS-Pi now reading the real ASCO state over the Group 5
serial link, confirm GenWatch sees the true switch state — **read-only, no
commands yet.**

1. On GenWatch, point at the ATS-Pi and enable read-only:
   ```bash
   sudo nano /etc/genwatch/config.yaml
   #   ats:
   #     enabled: true
   #     host: 192.168.1.250        # the ATS-Pi (not the ADAM)
   #     port: 5020                 # ATS-Pi default; use 502 only if the companion is set to it
   #     expected_unit_id: 23
   sudo systemctl restart genwatch
   journalctl -u genwatch -f        # expect "ATS-Pi integration enabled"
   ```
2. Confirm the block:
   ```bash
   curl -b cookies.txt http://localhost:8000/api/status | jq .ats
   ```
   - `comms.state == "healthy"`, `icdVersion == [1,0]`, `authoritative == true`,
     `atsPiUnitId` matches `expected_unit_id`.
3. **Cross-check against the physical switch:** the Live view ATS card
   `position` must match where the load actually is, and the Normal/
   Emergency availability chips must match the source breakers. Toggle a
   source breaker (if safe/planned) and confirm the chip follows.
4. **Run the automated acceptance test** — a scripted pass/fail gate that
   backs the manual cross-checks above with the full functionality +
   safety suite, straight from the GenWatch Pi (stdlib only, no venv):
   ```bash
   # bench (simulated H-100 + live ATS-Pi):
   GENWATCH_TEST_PASSWORD='<admin pw>' sudo -E python3 \
     deploy/scripts/acceptance_test.py --expect-mock true \
     --expected-unit-id 23 --local-checks
   # production cutover (real H-100): use --expect-mock false
   ```
   It is **non-actuating** — it verifies the auth, CSRF, and confirm-token
   gates without ever sending a valid confirm token, so it cannot start
   the generator or drive the ATS even if a guard were broken. A clean run
   prints `VERDICT: SAFE TO PROCEED` and exits 0 (so it can gate a
   commissioning script). See the script header for the safety model and
   the opt-in `--actuate-mock-generator` flag (simulated H-100 only).

- **PASS:** ATS card reflects the real switch position + source
  availability; comms healthy + authoritative.
- **If it fails:** position wrong → check the Group 5 RS-485 status-register/
  bit map (companion `io.asco_serial` config + the RS-485 bench-verify guide)
  or the serial link; not authoritative → ICD version or unit-id mismatch
  (`journalctl`); comms lost → `nc -zv 192.168.1.250 <port>` and the
  GenWatch↔ATS-Pi VLAN/ACL.

☐ Phase 7 complete — ATS read side matches reality — signed ______________ date __________

---

## Phase 8 — ATS command verification (ACTUATING — planned outage, golden sequence)

**Goal:** prove operator-issued ATS commands act correctly end-to-end.
Run during a **planned outage window** with the operator present. Follow
**ICD §13** as the authoritative sequence; the checklist below is the
on-site condensation.

1. **Test transfer:** from the Live view, **Test** → confirm → the ATS
   performs a test transfer (observe at the switch) → load returns to
   utility automatically. Events feed shows the chain.
2. **Inhibit:** assert **Inhibit**; with utility healthy, simulate a
   utility dropout (planned) and confirm the ATS does **not** transfer
   while inhibited; release → normal logic resumes.
3. **Force-Transfer (admin):** with the override warning shown, assert →
   load moves to generator; release → returns. Confirm it's **admin-gated**
   (a non-admin/operator session is rejected `403`).
4. **Comms-loss auto-release end-to-end (ICD §13 / §8.3):** assert
   Inhibit, then **stop GenWatch** (`sudo systemctl stop genwatch`); within
   ~30 s the ATS-Pi releases the relay on its own. Restart GenWatch.
5. **Full utility-outage cycle:** open the utility breaker → observe
   `UTILITY_LOST` → engine starts → load on **GENERATOR** → close utility →
   after the ASCO retransfer delay, load back on **UTILITY** → engine
   cool-down. The whole chain appears in the Events feed within ~15 s of
   each physical change, and Slack (if configured) gets the alerts.

- **PASS:** all five behave exactly as described; force-transfer is
  admin-only; the 30 s auto-release fires; the outage cycle is fully
  observable in the feed.

☐ Phase 8 complete — ATS commands + golden sequence verified — signed ______________ date __________

---

## 9. Production cutover & sign-off

- [ ] `journalctl -u genwatch --since "1 hour ago"` shows no unexpected
      errors or comms drops
- [ ] Backup taken of `/etc/genwatch/config.yaml` (contains secrets) and
      `/var/lib/genwatch/db.sqlite`
- [ ] Security hardening applied for the deployment (README §8 —
      Tailscale/Caddy/ufw as appropriate); **not** exposed to the public
      internet on plain HTTP
- [ ] Hardware watchdog confirmed active: `systemctl show -p RuntimeWatchdogUSec`
      is non-zero
- [ ] Both Pis on NTP, skew < 1 s (`chronyc tracking` on each)
- [ ] Operator team trained on the controls, the **panel key-switch
      interlock**, and the ATS precedence (ATS-Pi authoritative when healthy)
- [ ] This runbook completed with all phase sign-offs
- [ ] Deploying a **tagged release candidate** (e.g. `v0.1.0-rc1`) cut at the
      exact commissioned commit — **not** a moving `main` — with CI green at
      that commit. GenWatch and ats-pi-companion run their **matching** tags.

### Release candidate (deploy a tag, never bare `main`)

Cut a tag at the commit you are commissioning (CI green; this runbook worked
end to end) and deploy only that tag, so the plant always runs a known,
reproducible build with a clean rollback target:

```bash
git tag -a v0.1.0-rc1 -m "GenWatch RC1 — commissioned <site>, <date>"
git push origin v0.1.0-rc1
# Deploy this exact tag (and the matching ats-pi-companion tag) to the Pis.
```

Record the deployed build below; this is the artifact the sign-off attests to.

| Component | Tag | Commit SHA |
|---|---|---|
| GenWatch | | |
| ats-pi-companion | | |

### Final acceptance

| | Name | Signature | Date |
|---|---|---|---|
| Commissioning engineer | | | |
| Electrician (if ATS wired) | | | |
| Site operator / owner | | | |

---

## Appendix A — Rollback / abort

| Situation | Action |
|---|---|
| Anything wrong during an actuating phase | Operator turns the H-100 key switch to **OFF/MANUAL** (kills remote control instantly) and/or local E-stop |
| H-100 register map suspect | Service stays read-only-safe; fix `h100.yaml`, `…/api/registers/reload`. No restart needed. |
| ATS misbehaving | Set `ats.enabled: false`, `systemctl restart genwatch` → falls back to H-100-derived load source; the ATS keeps running on its **own** automatic logic regardless |
| ATS-Pi / ADAM suspect | Power down the ATS-Pi — the ASCO operates normally on its own controller; GenWatch falls back to H-100 telemetry |
| Maintained ATS command stuck | Kill the GenWatch session; the ATS-Pi auto-releases within 30 s (ICD §8.3). If the ATS-Pi itself is unresponsive, the ADAM's FSV opens the relays within its timeout (Phase 5 step 2). Ultimate manual fallback: **pull the ADAM's 24 VDC supply** — every relay is wired COM–NO, so de-energized = released. |

## Appendix B — Quick command reference

```bash
# H-100 side
sudo genwatch doctor                                    # config + DB + live probe
sudo -u genwatch genwatch panel [--html]                # decoded snapshot vs the LCD
sudo -u genwatch genwatch modbusdump --addr 0xNN --count N
sudo -u genwatch genwatch scan --start 0x0000 --end 0x07FF
journalctl -u genwatch -f

# ATS side
[ATS-Pi] modpoll -m tcp -a 1 -r 1 -c 1 192.168.1.251    # raw ADAM packed DI
[laptop] modpoll -m tcp -a 1 -r 1 -c 8 192.168.1.250    # ATS-Pi ICD core state
curl -b cookies.txt http://localhost:8000/api/status | jq .ats
[ATS-Pi] journalctl -u atspi -f
```

*Pin this to the enclosure door after sign-off.*
