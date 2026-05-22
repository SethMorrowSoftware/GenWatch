# GenWatch

Professional monitoring and control software for the **Generac H-100** industrial generator, running on a **Raspberry Pi 5**.

A single-pane operator console with live engine state, electrical output, two-step-confirm controls (start / stop / quiet-test / transfer), time-series history, alarms, and on-device configuration of the serial port, register map, and retention policy. Communicates with the H-100 controller over Modbus RTU via a USB-to-RS485 adapter.

![architecture diagram — see docs/HARDWARE for wiring](#)

> **Reliability:** All 28 unit + end-to-end tests pass. systemd hardened unit with watchdog. Service refuses to start if the RS-485 link is unreachable (no silent fallback to mock). Login rate-limited. SQLite WAL with crash-safe retention. Audit log on every control command.

---

## Table of contents

1. [What you need (Bill of Materials)](#1-what-you-need-bill-of-materials)
2. [Wiring the RS-485 link](#2-wiring-the-rs-485-link)
3. [Prepare the Raspberry Pi 5](#3-prepare-the-raspberry-pi-5)
4. [Install GenWatch](#4-install-genwatch)
5. [Initial configuration](#5-initial-configuration)
6. [Verify the Modbus link](#6-verify-the-modbus-link)
7. [Operation](#7-operation)
8. [Security recommendations](#8-security-recommendations)
9. [Updating GenWatch](#9-updating-genwatch)
10. [Troubleshooting](#10-troubleshooting)
11. [Architecture overview](#11-architecture-overview)
12. [Adapting the register map](#12-adapting-the-register-map)
13. [Development](#13-development)
14. [License](#14-license)

---

## 1. What you need (Bill of Materials)

Approximate total: **$150–$250 USD** depending on enclosure and adapter choice.

### Required

| # | Item | Why | Recommended |
|---|------|-----|-------------|
| 1 | Raspberry Pi 5 (4 GB or 8 GB) | The host computer. 4 GB is plenty; 8 GB if you want headroom. | [Raspberry Pi 5 — 4GB](https://www.raspberrypi.com/products/raspberry-pi-5/) |
| 2 | Raspberry Pi 27 W USB-C power supply | Pi 5 needs 5 V / 5 A. Cheap chargers cause brownouts and under-voltage warnings. | Official Pi 27 W PSU |
| 3 | Active cooler for Pi 5 | Pi 5 throttles aggressively without active cooling, especially in an outdoor/cabinet enclosure. | Official Pi 5 Active Cooler |
| 4 | microSD card, 32 GB+, A2 class | OS + database storage. A2 cards have markedly better random-write IOPS — important for SQLite. | SanDisk Extreme Pro 64 GB A2, or Samsung Pro Endurance |
| 5 | USB-to-RS485 adapter | Bridges the Pi's USB to the H-100 controller's RS-485 bus. Get one with hardware auto-direction and FTDI/CH340/CP210x chipset. | FTDI USB-RS485-WE-1800-BT, or DSD TECH SH-U10 (CH340), or Waveshare USB-to-RS485 (FT232) |
| 6 | Twisted-pair shielded cable, 22-24 AWG | The RS-485 differential pair plus shield/drain. Belden 9841 (or equivalent) is the industry standard. Length up to ~1000 m at 9600 baud. | Belden 9841 (one twisted pair + shield) |
| 7 | Two 120 Ω 1/4 W resistors | Bus termination at both physical ends (Pi end and H-100 end). Many USB-RS485 adapters have a built-in terminator — check before buying extras. | Standard 1/4 W, ±5 % carbon-film |
| 8 | Pi 5 case with cooling cut-outs | Mechanical protection inside the generator cabinet. Argon NEO 5 BRED, Pironman, or a sealed DIN-rail enclosure for industrial install. | Argon NEO 5 BRED (active-cooler compatible) |

### Optional but recommended

| Item | Why |
|------|-----|
| **NVMe SSD + Pi 5 M.2 HAT** (PCIe HAT + 256 GB+ NVMe) | Much faster + more durable than microSD. SQLite + journal rotates cleanly. Reduces SD-wear failures over multi-year deployments. |
| **UPS HAT** (Waveshare UPS HAT (E) or PiSugar) | Survives utility-side outages without filesystem corruption. Especially relevant since the Pi is monitoring a *generator* — utility loss is the interesting event. |
| **Touchscreen** (Official Pi 7" Touch Display 2) | Wall-mounted in the generator room as a HMI. The UI is responsive down to 1024 × 600. |
| **Tailscale subscription** (free for ≤ 3 users) | Secure remote access without exposing the Pi to the public internet. See [§8 Security](#8-security-recommendations). |
| **DIN-rail mount** | For control-panel installation. |
| **Wago lever-nut connectors** (221-412) | Clean, screwless wiring at the controller terminal. |

### Compatible adapter chipsets (any will work)

Plug-and-play under the bundled udev rule (no extra config — they all show up as `/dev/genwatch-rs485`):

- **FTDI FT232/FT231X/FT232H** (USB VID 0x0403) — most reliable, slightly more expensive
- **WCH CH340/CH341** (VID 0x1A86) — cheap, generally works fine on Bookworm
- **Silicon Labs CP2102/CP2104** (VID 0x10C4) — good middle ground
- **Prolific PL2303** (VID 0x067B) — works but avoid clones with sketchy drivers

If you're buying new, the **DSD TECH SH-U10** (CH340) is ~$10 and has been reliable in field deployments. For mission-critical sites prefer a true FTDI chip.

---

## 2. Wiring the RS-485 link

The H-100 controller exposes Modbus RTU on a 3-terminal RS-485 port (label varies by panel: "Modbus", "RS-485", "External Comms", or "Mod-485").

```
   USB-RS485 adapter             H-100 controller
   ─────────────────             ────────────────
   A  (D+ / TX+)     ───────►    A  (D+)
   B  (D− / TX−)     ───────►    B  (D−)
   GND (signal gnd)  ───────►    COM / GND
                                 │
                                 ├── 120 Ω termination at this end
                                 │   (if not already factory-installed)
   120 Ω termination ─────┘ also at the adapter end (most have a jumper)
```

### Wiring rules

1. **A↔A, B↔B**: If the line is dead, swap A and B at one end. Half of all RS-485 problems are polarity swaps.
2. **Twisted pair only**: A and B must be the *same* twisted pair (e.g. blue / blue-white in Belden 9841). Don't use ribbon cable.
3. **Single shield ground**: Connect the shield/drain wire to GND at *one* end only (the Pi end) — grounding both ends creates a ground loop and adds noise.
4. **120 Ω termination at both physical ends of the bus**: not in the middle. The Pi-side adapter usually has a built-in 120 Ω termination resistor that you can enable with a jumper or DIP switch. The H-100 end needs an external 120 Ω resistor across A↔B if there isn't already one fitted internally (check the panel manual or measure with a multimeter — see §10 Troubleshooting).
5. **No daisy-chain spurs > 30 cm**: For a long run, the bus must be linear (Pi → controller). Don't tee off branches.
6. **Keep away from high-voltage runs**: Don't run the RS-485 cable inside conduit with the generator output bus. Cross at 90° if you have to.

### Recommended H-100 panel settings (verify against your firmware)

| Setting | Value |
|---------|-------|
| Modbus slave address | **100** (`0x64`) — factory default |
| Baud rate | **9600** |
| Data bits | **8** |
| Parity | **None** |
| Stop bits | **1** |
| Function code (read) | **3** (read holding registers) |

These are the GenWatch defaults too — no changes needed if your H-100 ships from the factory.

---

## 3. Prepare the Raspberry Pi 5

### 3.1 Install Raspberry Pi OS Bookworm (64-bit)

1. Download [Raspberry Pi Imager](https://www.raspberrypi.com/software/) on a desktop computer.
2. Insert the microSD (or your NVMe via a USB adapter).
3. Choose:
   - Device: **Raspberry Pi 5**
   - OS: **Raspberry Pi OS (64-bit)** → *Raspberry Pi OS Lite (64-bit)* recommended if you don't need the desktop. Standard works too.
4. Click the gear icon → **OS customization** and set:
   - Hostname: `genwatch` (so it's reachable at `genwatch.local` via mDNS)
   - Username + password (this is the Pi's *Linux* login, distinct from GenWatch's operator login)
   - SSID/password for Wi-Fi (or skip if using Ethernet)
   - Enable SSH with password authentication
   - Locale, timezone
5. **Write** the image.

### 3.2 First boot

Power up the Pi and SSH in:

```bash
ssh <user>@genwatch.local
```

Run system updates:

```bash
sudo apt-get update && sudo apt-get -y upgrade
sudo reboot
```

### 3.3 (Optional) Disable the on-board Bluetooth UART

If you happen to be wiring the H-100 to the Pi's GPIO UART pins instead of using a USB-RS485 adapter (advanced — most users should not do this), the Pi 5 needs the GPIO UART explicitly enabled. **For the standard USB-RS485 setup documented here, skip this step.**

To enable GPIO UART:
```bash
sudo raspi-config nonint do_serial_hw 0      # enable hardware UART
sudo raspi-config nonint do_serial_cons 1    # disable login shell on it
sudo reboot
```
The on-board UART then appears as `/dev/ttyAMA10` on Pi 5 (different from Pi 4's `/dev/ttyAMA0`). Set `serial.device: /dev/ttyAMA10` in `config.yaml`.

---

## 4. Install GenWatch

Plug your USB-RS485 adapter into one of the Pi's USB ports, then:

```bash
# Clone the repo
git clone https://github.com/sethmorrowsoftware/genwatch.git ~/genwatch
cd ~/genwatch

# Run the installer
sudo deploy/scripts/install.sh
```

The installer does the following idempotently — re-run any time you pull updates:

1. Verifies you're root, on Bookworm, on a Pi.
2. Installs apt deps: `python3-venv`, `build-essential`, `nodejs` (>=18), `npm`, `rsync`.
3. Creates the `genwatch` system user, adds it to the `dialout` group (for serial access).
4. Builds the React/TypeScript frontend (`vite build` — takes ~30 s on Pi 4, ~10 s on Pi 5).
5. Creates the Python venv at `/opt/genwatch/venv` and installs backend deps.
6. Copies the backend package to `/opt/genwatch/genwatch/`.
7. Copies the built frontend to `/usr/share/genwatch/ui/`.
8. Installs the udev rule that symlinks any supported RS-485 adapter to `/dev/genwatch-rs485`.
9. Provisions `/etc/genwatch/config.yaml` with a random `jwt_secret`.
10. Installs the systemd unit, runs the pre-flight diagnostics, and starts the service (after the admin password is set).

You should see something like:

```
[genwatch] Repository root: /home/pi/genwatch
[genwatch] Host:            Raspberry Pi 5 Model B Rev 1.0
[genwatch] OS:              debian-bookworm
[genwatch] Installing apt packages: python3-venv python3-dev …
[genwatch] node: v20.10.0
[genwatch] Creating system user genwatch …
[genwatch] Building frontend bundle (this can take ~30 s on a Pi 4) …
[genwatch] Installing udev rule for /dev/genwatch-rs485
[genwatch] Running pre-flight diagnostics
== GenWatch doctor (v0.1.0) ==
  Python:    3.11.x
  Config:    /etc/genwatch/config.yaml
  Mock:      False
  Auth:      MISSING admin_password_hash — run: genwatch hash <password>
  Registers: /opt/genwatch/genwatch/registers/h100.yaml
             18 read + 4 write, slave=100
  Serial:    /dev/genwatch-rs485 opens OK at 9600 8N1
  Modbus:    slave 100 responded with [2] (37ms)

⚠  ADMIN PASSWORD NOT SET
```

If you see `Modbus: NO RESPONSE`, jump to [§10 Troubleshooting](#10-troubleshooting). The installer continues regardless — the service just won't start until the admin password is set.

---

## 5. Initial configuration

### 5.1 Set the admin password

```bash
sudo genwatch hash 'pick-a-strong-password'
# → $2b$12$XJZ... (paste this whole line)
sudo nano /etc/genwatch/config.yaml
```

Find the `admin_password_hash:` line and replace `"REPLACE_ME"` with the hash you just generated. Save and exit.

### 5.2 Start the service

```bash
sudo systemctl restart genwatch
sudo systemctl status genwatch
```

You should see `active (running)`. If not, check the log:

```bash
journalctl -u genwatch -e --no-pager
```

### 5.3 Open the operator console

From any device on the same network:

```
http://genwatch.local:8000
```

(Use the Pi's IP address if `.local` mDNS isn't working — `hostname -I` on the Pi prints it.)

Log in with the password you set in §5.1.

### 5.4 (Optional) Verify telemetry is live

The Live view should populate within ~2 seconds with engine state, frequency, voltages, and currents from your H-100. The "Comms" badge in the top-right should be green and showing 100 % success.

---

## 6. Verify the Modbus link

The bundled `genwatch doctor` and `genwatch modbusdump` commands let you check the link end-to-end without touching the UI:

```bash
# Full pre-flight: config, serial port permissions, register map, DB, and a live Modbus probe
sudo genwatch doctor

# Read a sweep of 16 registers starting at 0x0001 (engine_state region)
sudo -u genwatch genwatch modbusdump --addr 0x0001 --count 16
# → 0x0001    2  0x0002      (engine_state — 2 = running)
# → 0x0002    0  0x0000      (alarm_state)
# → 0x0003    3  0x0003      (switch_state)
# ...

# Try the kW register specifically (default 0x0028)
sudo -u genwatch genwatch modbusdump --addr 0x0028 --count 1
```

If `modbusdump` returns values but they don't match what you see on the H-100 panel, your firmware revision uses different addresses. See [§12 Adapting the register map](#12-adapting-the-register-map).

---

## 7. Operation

### Daily use

The Live view is the operator console: engine state, electrical output, control buttons, recent events.

- **Remote Start**: only enabled when state is `stopped`. Two-step confirm with an 8-char hex token that expires in 30 s.
- **Remote Stop**: enabled while running/exercising. Initiates the controller's normal cool-down cycle.
- **Quiet-Test**: 30-minute unloaded exercise. Idle exercise schedule shown at the top right.
- **Transfer back**: while running, hand the load back to utility and cool down.

All commands write to the H-100's control registers (`0x00A0..A3`) via FC06 and are audit-logged with the operator, timestamp, register, value, and result.

### Views

- **Live** — Real-time operator console. Sparklines update every 1.5 s; main telemetry every 15 s.
- **History** — Chart of any metric over 10 min to 30 days. SQLite-backed, decimated server-side.
- **Events** — Append-only log of state transitions, alarms, comms changes, and operator commands.
- **Settings** — Serial port, Modbus, register map, retention. Changes saved to `/etc/genwatch/config.yaml`; the UI warns when a restart is required.

### CLI commands

All exposed via the `genwatch` wrapper installed by the installer:

```bash
genwatch serve                  # run the service (used by systemd)
genwatch hash <password>        # bcrypt-hash a password for config
genwatch gensecret              # generate a JWT signing secret
genwatch doctor                 # pre-flight diagnostics
genwatch modbusdump [--addr]    # read raw registers from the controller
genwatch version                # print version
```

### Useful systemd commands

```bash
sudo systemctl restart genwatch         # restart after config changes
sudo systemctl stop genwatch            # stop
sudo systemctl status genwatch          # status + last 10 log lines
journalctl -u genwatch -e               # follow the log (press q to quit)
journalctl -u genwatch --since "10 min ago"
```

---

## 8. Security recommendations

GenWatch is designed for a **trusted LAN** deployment. By default it listens on `0.0.0.0:8000` over plain HTTP; cookies are not `Secure`. This is appropriate for a Pi sitting in the same building as the generator on a private network. Do not expose port 8000 to the public internet without the following:

### 8.1 Use Tailscale for remote access

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

Tailscale gives you an encrypted private mesh; the Pi gets an IP like `100.x.y.z` reachable only from your other Tailscale devices. Combined with [Tailscale ACLs](https://tailscale.com/kb/1018/acls) this is more than sufficient for most field deployments. You can also enable HTTPS via Tailscale's `tailscale cert` if you want browser-trusted TLS.

### 8.2 Or terminate TLS with Caddy in front of GenWatch

```bash
sudo apt-get install -y caddy
sudo tee /etc/caddy/Caddyfile <<EOF
genwatch.your-domain.example {
    reverse_proxy localhost:8000
}
EOF
sudo systemctl restart caddy
```

Caddy will auto-fetch a Let's Encrypt cert if the hostname resolves publicly. Then change the GenWatch cookie to `secure=True` (line 30 in `backend/genwatch/api/auth.py`).

### 8.3 Firewall

```bash
sudo apt-get install -y ufw
sudo ufw allow ssh
sudo ufw allow from 192.168.0.0/16 to any port 8000
sudo ufw enable
```

Restricts the GenWatch port to your LAN ranges.

### 8.4 Built-in defenses

- **Login rate-limiter**: 5 attempts then 1 attempt per 3 minutes per source IP. State resets on service restart.
- **JWT secret regeneration**: invalidate all sessions by regenerating: `sudo genwatch gensecret` → paste into config.yaml `jwt_secret:` → `sudo systemctl restart genwatch`.
- **Audit log** in `/var/lib/genwatch/db.sqlite` table `audit` records every login attempt, confirm token issue/use, and control command with the source IP, operator, and result.
- **Server-side state validity**: even if the UI bug-allows clicking "Start" while the engine is running, the server rejects with HTTP 409 and audit-logs the denial.

---

## 9. Updating GenWatch

The installer is idempotent. To upgrade:

```bash
cd ~/genwatch
git pull
sudo deploy/scripts/install.sh
sudo systemctl restart genwatch
```

The installer will:
- Reinstall apt deps (no-op if current).
- Reinstall the venv deps (only changes if requirements.txt moved).
- Rebuild the frontend.
- Sync the backend package.
- Keep your `/etc/genwatch/config.yaml` and `/var/lib/genwatch/db.sqlite` untouched.

The SQLite schema is forward-compatible — `CREATE TABLE IF NOT EXISTS` everywhere — so an upgrade never destroys data.

---

## 10. Troubleshooting

### Symptom: `Modbus: NO RESPONSE` in `genwatch doctor` / "Comms lost" in UI

A live Modbus link should respond within ~50 ms. If it doesn't:

| Check | Command / action |
|-------|------------------|
| Adapter plugged in and recognized? | `lsusb` should list it. `ls -l /dev/genwatch-rs485` should show a symlink to `/dev/ttyUSB0` or similar. |
| Wrong polarity (A/B swapped)? | Most common. Swap the two wires at the controller end and re-test. |
| Wrong baud rate or slave ID? | Verify on the H-100 panel itself. Mismatched baud = silence, mismatched slave ID = exception code 11 or silence. |
| No termination? | Across A↔B at the controller end (and Pi end if your adapter has no built-in terminator). The line resistance A↔B should measure ~60 Ω with both terminators in (two 120 Ω in parallel). With no termination the bus looks like ~∞ Ω. |
| Adapter doesn't auto-direction? | Cheap adapters need an RTS pin to toggle TX/RX direction. Buy one with hardware auto-direction (any of the chipsets in §1). |
| Conflicting Modbus master? | If a Generac MLink or similar is already polling the same RS-485 bus, you'll see intermittent collisions. Disconnect the other master or use a separate port. |
| Cable too long? | At 9600 baud, ~1000 m is the theoretical limit. In practice keep under 300 m on consumer cable. |

### Symptom: `Serial: CANNOT OPEN /dev/genwatch-rs485 — Permission denied`

The genwatch user must be in the `dialout` group:

```bash
groups genwatch     # should include 'dialout'
# If not:
sudo usermod -aG dialout genwatch
sudo systemctl restart genwatch
```

### Symptom: `Serial: /dev/genwatch-rs485 DOES NOT EXIST`

The udev rule didn't match your adapter. List what's there:

```bash
ls -l /dev/ttyUSB* /dev/ttyACM* 2>/dev/null
lsusb -v 2>/dev/null | grep -E "idVendor|idProduct|iProduct"
```

Either:
- Add your VID:PID to `/etc/udev/rules.d/99-genwatch-rs485.rules` and run `sudo udevadm control --reload-rules && sudo udevadm trigger`, OR
- Set `serial.device: /dev/ttyUSB0` (or whatever path lsusb showed) explicitly in `/etc/genwatch/config.yaml`.

### Symptom: Service flapping (restarting every minute or so)

```bash
journalctl -u genwatch --since "5 minutes ago" --no-pager
```

The systemd unit has a watchdog set to 60 s; if the app's poller hangs (e.g. waiting forever on a serial read) the watchdog will SIGKILL and systemd restarts the service. The boot event log in the DB shows the boot pattern. If the underlying problem is the RS-485 link going down and pymodbus blocking, run `genwatch doctor` while the service is stopped to isolate.

### Symptom: SQLite "database is locked"

WAL mode handles concurrent reads fine. Locks only happen if a foreign process (e.g. you opened the DB with `sqlite3` and started a transaction) is holding a write lock. `Ctrl-D` out of that and try again. The service can still read while you peek:

```bash
sudo -u genwatch sqlite3 /var/lib/genwatch/db.sqlite "SELECT * FROM events ORDER BY ts DESC LIMIT 10;"
```

### Symptom: "Connection refused" in the browser

```bash
sudo systemctl status genwatch       # is it running?
sudo ss -tlnp | grep 8000             # is it listening on 8000?
```

If the service is `failed`, `journalctl -u genwatch -e` will show why. The most common reason is `Modbus serial connect failed` — refer to the first row of this table.

### Symptom: Under-voltage warnings, kernel messages about power

Pi 5 needs a true 5 V / 5 A supply. Cheap USB-C chargers brown out under USB peripheral load. Use the official 27 W PSU, or measure with a USB power meter (should hold 5.1 V).

---

## 11. Architecture overview

```
┌─────────────────────────────────────────────────────────────────┐
│ Browser (Chrome/Safari/Firefox)                                 │
│  React + TypeScript SPA — Live / History / Events / Settings   │
└───────────┬────────────────────────────────────────┬────────────┘
            │ HTTPS (or HTTP on LAN)                  │ WebSocket
            │ REST: /api/*                            │ /ws/live
            v                                         v
┌─────────────────────────────────────────────────────────────────┐
│ Raspberry Pi 5  ·  systemd unit: genwatch.service               │
│                                                                  │
│  FastAPI + uvicorn (single worker — Modbus is single-master)   │
│   ├─ /api/auth   login, logout, /me                              │
│   ├─ /api/status full live snapshot                              │
│   ├─ /api/telemetry  time-series (SQLite-backed)                 │
│   ├─ /api/events     event/alarm log                             │
│   ├─ /api/control    confirm-token-gated start/stop/etc.         │
│   ├─ /api/config     read/write /etc/genwatch/config.yaml        │
│   └─ /api/registers  read/reload register map                    │
│                                                                  │
│  Two-tier Modbus poller:                                         │
│   • prime (1.5 s): engine_state, alarm_state, switch_state       │
│   • base  (15 s):  RPM, V, A, Hz, kW, oil P, coolant, batt,…    │
│  Coalesces contiguous registers into a single Modbus read.       │
│                                                                  │
│  State machine + control service:                                │
│   • semantic engine state (stopped/cranking/running/…)           │
│   • two-step confirm tokens (8-char hex, 30 s TTL, single-use)   │
│   • server-side state-validity guards                            │
│   • audit log on every command                                   │
│                                                                  │
│  Storage (SQLite WAL):                                           │
│   • telemetry / telemetry_1m / telemetry_1h                      │
│   • events / alarms_active / audit / kv                          │
│   • retention task aggregates and prunes every 5 min             │
└─────────────────────────────┬───────────────────────────────────┘
                              │ Modbus RTU (RS-485, 9600 8N1)
                              v
                        ┌──────────────┐
                        │ H-100        │ Generac H-100 controller
                        │ controller   │ on the generator panel
                        └──────────────┘
```

### Reliability features

- **systemd watchdog**: `Type=notify`, `WatchdogSec=60s`. The app pings `sd_notify(WATCHDOG=1)` every 30 s while the poller loop is healthy. If the poller hangs (e.g. a pymodbus deadlock on a flaky link), systemd SIGKILLs and restarts within 60 s.
- **Refuse-to-start on RS-485 failure**: in production the service exits cleanly with a clear error if it can't open the serial port. Operators see "service down", not a silent simulator.
- **Hardened unit**: `NoNewPrivileges`, `ProtectSystem=strict`, `ProtectKernelTunables`, narrow `DeviceAllow` list, `MemoryMax=512M`, `TasksMax=128`.
- **WAL-mode SQLite**: crash-safe, survives Pi power loss without corruption.
- **Per-poll timeouts and retries** on every Modbus read.
- **Comms watchdog**: declares LOST after no successful prime poll for 3× the prime cadence.
- **Token replay protection**: confirm tokens are single-use, 30 s TTL, operator-bound, audit-logged on every state transition.
- **Server-side state validity**: every control command re-checks the engine state and rejects with 409 Conflict if invalid (e.g. Start while running).
- **Login rate-limiter**: token-bucket per source IP, 5 burst then 1 per 3 min.
- **Retention**: raw telemetry pruned at 7 d, 1-min rollup at 90 d, info events at 30 d. Alarms/warns and audit log are never auto-pruned.

---

## 12. Adapting the register map

The shipped `backend/genwatch/registers/h100.yaml` matches the addresses in Generac's reference docs for current H-100 firmware. Older or dealer-customized firmware may differ. To find your real addresses:

```bash
# Read a sweep of 16 holding registers starting at 0x0001
sudo -u genwatch genwatch modbusdump --addr 0x0001 --count 16

# Probe common H-100 register regions
for a in 0x0001 0x0010 0x0020 0x0028 0x0030 0x00A0; do
  sudo -u genwatch genwatch modbusdump --addr $a --count 8
done
```

Cross-reference the values you see with what the H-100 panel shows on its own screen. When you have the right addresses, edit:

```bash
sudo nano /opt/genwatch/genwatch/registers/h100.yaml
```

Then hot-reload (admin auth required):

```bash
curl -b cookies.txt -X POST http://localhost:8000/api/registers/reload
```

Or restart the service to fully rebind the poller batching:

```bash
sudo systemctl restart genwatch
```

The YAML schema is documented in comments at the top of `h100.yaml` — `addr`, `fc`, `type` (`u16`/`s16`/`u32`/`s32`/`bitfld`/`enum`), `scale`, `tier` (`prime`/`base`), `group`, `unit`, `warn_range`, `alarm_range`.

---

## 13. Development

### Local development (no hardware)

```bash
# Clone
git clone https://github.com/sethmorrowsoftware/genwatch.git
cd genwatch

# Backend
cd backend
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
GENWATCH_MOCK=true \
GENWATCH_AUTH__JWT_SECRET=$(.venv/bin/python -m genwatch gensecret) \
GENWATCH_AUTH__ADMIN_PASSWORD_HASH=$(.venv/bin/python -m genwatch hash dev) \
.venv/bin/python -m genwatch serve
# → http://127.0.0.1:8000/api/health

# Frontend (separate terminal)
cd frontend
npm install
npm run dev      # → http://127.0.0.1:5173 (proxies /api + /ws to :8000)

# Login with password "dev"
```

The mock client simulates a plausible H-100 — engine state machine, electrical output, alarm injection. Control buttons drive the mock, so the full operator flow works without an RS-485 adapter.

### Tests

```bash
cd backend
.venv/bin/pip install pytest==8.3.4 pytest-asyncio==0.25.0 httpx==0.28.1
.venv/bin/python -m pytest tests/ -v
# 28 tests: register decode + batching, e2e mock control flow, rate-limit,
# events retention, sd_notify, refuse-to-start safety
```

### Layout

```
backend/
  genwatch/
    modbus/          register YAML loader, decoder, RTU client, two-tier poller
    services/        state machine, control, auth, retention, rate-limit, notify
    api/             REST + WebSocket routes
    registers/       h100.yaml — default register map
  tests/             pytest

frontend/
  src/
    api/             typed fetch client
    hooks/           useLiveData (WS + status seed + reconnect backoff)
    components/      Icon, Pill, Sparkline, LineChart, Card, Modal, Switch
    views/           Live, History, Events, Settings, Login, ConfirmModal
    styles/          genwatch.css (from design handoff)

deploy/
  systemd/genwatch.service    Hardened unit with sd_notify watchdog
  udev/99-genwatch-rs485.rules Stable /dev/genwatch-rs485 symlink
  scripts/install.sh           Idempotent installer
  config.yaml.example          Annotated config template

design_handoff_genwatch/       Original design spec (reference)
```

### API contract

| Method | Path                          | Notes                              |
|--------|-------------------------------|------------------------------------|
| GET    | `/api/health`                 | Liveness; no auth.                  |
| POST   | `/api/auth/login`             | `{ password }` → session cookie     |
| POST   | `/api/auth/logout`            | Clear cookie                        |
| GET    | `/api/auth/me`                | Identity (200 even when anonymous) |
| GET    | `/api/status`                 | Full live snapshot                  |
| GET    | `/api/telemetry`              | `?metric=kw&from=&to=&max_points=` |
| GET    | `/api/events`                 | `?limit=&severity=alarm,warn`       |
| GET    | `/api/alarms?active=true`     | Active alarms                       |
| POST   | `/api/alarms/{code}/ack`      | Operator clears an alarm            |
| GET    | `/api/alarm-codes`            | Static reference table              |
| GET    | `/api/control/confirm`        | Issue confirm token (op+)           |
| POST   | `/api/control/{start,stop,exercise,transfer}` | Body `{ confirm_token }` |
| GET    | `/api/config`                 | Effective config (sanitized)        |
| PUT    | `/api/config`                 | Update on-disk config (admin)       |
| GET    | `/api/registers`              | Current register map + last read    |
| POST   | `/api/registers/reload`       | Re-read YAML from disk (admin)      |
| WS     | `/ws/live`                    | `snapshot` / `transition` / `alarm` |

All errors return JSON `{ detail: { code, message } }` with appropriate HTTP status.

---

## 14. License

MIT — see [LICENSE](LICENSE) (add one before shipping).
