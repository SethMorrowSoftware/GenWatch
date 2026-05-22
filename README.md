# GenWatch

Professional monitoring and control software for the **Generac H-100** industrial generator, running on a **Raspberry Pi 5**.

A single-pane operator console with live engine state, electrical output, two-step-confirm controls (start / stop / quiet-test / transfer), time-series history, alarms, and on-device configuration of the serial port, register map, and retention policy. Communicates with the H-100 controller over Modbus RTU.

**Physical layer:** the H-100 has both an **RS-232** port (factory-default Modbus *slave*, 9600 8N1, address 100 — this is what GenLink uses, and what GenWatch uses by default) and an **RS-485** port (factory-default Modbus *master* to remote annunciators and HTS transfer switches at 4800 8N2 — not directly usable until the panel is reconfigured). The default install path documented below targets the RS-232 port because that's how the H-100 ships from the factory. An advanced RS-485 path is documented in [§2.5](#25-advanced-rs-485-instead-of-rs-232).

![architecture diagram — see docs/HARDWARE for wiring](#)

> **Reliability:** All 28 unit + end-to-end tests pass. systemd hardened unit with sd_notify watchdog. Service refuses to start if the Modbus link is unreachable (no silent fallback to mock). Login rate-limited. SQLite WAL with crash-safe retention. Audit log on every control command.

---

## Table of contents

1. [What you need (Bill of Materials)](#1-what-you-need-bill-of-materials)
2. [Wiring the Modbus link](#2-wiring-the-modbus-link)
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

### Required — recommended (default) RS-232 path

This is the path that matches the H-100 as it ships from Generac. No panel reconfiguration needed.

| # | Item | Why | Recommended |
|---|------|-----|-------------|
| 1 | Raspberry Pi 5 (4 GB or 8 GB) | The host computer. 4 GB is plenty; 8 GB if you want headroom. | [Raspberry Pi 5 — 4 GB](https://www.raspberrypi.com/products/raspberry-pi-5/) |
| 2 | Raspberry Pi 27 W USB-C power supply | Pi 5 needs 5 V / 5 A. Cheap chargers cause brownouts and under-voltage warnings. | Official Raspberry Pi 27 W PSU |
| 3 | Active cooler for Pi 5 | Pi 5 throttles aggressively without active cooling, especially in an outdoor/cabinet enclosure. | Official Raspberry Pi 5 Active Cooler |
| 4 | microSD card, 32 GB+, A2 class | OS + database storage. A2 cards have markedly better random-write IOPS — important for SQLite. | SanDisk Extreme Pro 64 GB A2, or Samsung Pro Endurance 64 GB |
| 5a | **Generac 0F7707 PC interface cable** (recommended) | The factory service cable. Gray "Computer" end on the PC side, Black "Control Panel" end on the H-100 RS-232 port. Handles the panel-side connector + null-modem crossover for you. | Generac part **0F7707** (sold through Generac dealers) |
| 5b | **USB-to-DB9 serial adapter** with a quality chipset | Bridges the Pi's USB to the gray DB9 end of the 0F7707. Avoid no-name PL2303 clones — they're driver-unstable on modern Linux. | StarTech **ICUSB232V2** (FTDI), Tripp Lite **USA-19HS** (Keyspan), or any FTDI-FT232R-based USB-DB9 cable |
| 6 | Pi 5 case with cooling cut-outs | Mechanical protection inside the generator cabinet. Argon NEO 5 BRED, Pironman 5, or a sealed DIN-rail enclosure for industrial install. | Argon NEO 5 BRED (active-cooler compatible) |

> If the Generac 0F7707 cable isn't available, you can build an equivalent: USB-DB9 (FTDI) cable + **DB9 female-female null-modem adapter** + DB9-to-(panel connector) extension. The 0F7707 saves you the wiring research, though, and is the supported configuration. RS-232 max cable length is ~15 m at 9600 baud — beyond that, use the RS-485 path in [§2.5](#25-advanced-rs-485-instead-of-rs-232).

### Alternative — RS-485 path (long runs, multi-device, requires panel reconfig)

Use this if the Pi is more than ~15 m of cable from the H-100, or if you need to drop the Pi onto an existing RS-485 SCADA bus. Requires reconfiguring the H-100's RS-485 port from "master" to "slave" via GenLink and disconnecting any annunciators/HTS-485 peripherals from that port.

| # | Item | Why | Recommended |
|---|------|-----|-------------|
| 5' | **USB-to-RS485 adapter** with hardware auto-direction and a quality chipset | Bridges the Pi's USB to the H-100 RS-485 terminal block (A/B/GND). | FTDI **USB-RS485-WE-1800-BT**, DSD TECH **SH-U10** (CH340), Waveshare **USB to RS485** (FT232) |
| 6' | Twisted-pair shielded cable, 22-24 AWG | The RS-485 differential pair plus shield/drain. Up to ~1000 m at 9600 baud, ~300 m typical with consumer cable. | Belden **9841** (one twisted pair + shield) |
| 7' | Two 120 Ω 1/4 W resistors | Bus termination at both physical ends of the linear bus. Many USB-RS485 adapters have a built-in terminator switchable by a DIP — check before buying extras. | Standard 1/4 W, ±5 % carbon-film |

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

The bundled udev rule symlinks any of these adapters to `/dev/genwatch-modbus` (covers both RS-232 cables and RS-485 modules — they use the same USB-to-serial bridge chips):

- **FTDI FT232 / FT231X / FT232H** (USB VID 0x0403) — most reliable, the safe choice for mission-critical sites
- **Silicon Labs CP2102 / CP2104** (VID 0x10C4) — good middle ground, well-supported on Linux
- **WCH CH340 / CH341** (VID 0x1A86) — cheap, generally works fine on Bookworm; budget-friendly
- **Prolific PL2303** (VID 0x067B) — works in principle, but **avoid no-name clones** — many ship with counterfeit chips that get blacklisted by recent Linux kernels. If you can't tell, get FTDI instead.

For a field-deployed monitoring station that's expected to run for years, **buy an FTDI-based adapter.** The ~$15 premium is paid back the first time you don't have to drive out to a generator pad because a Prolific clone got blacklisted by a kernel update.

---

## 2. Wiring the Modbus link

The H-100 has both an RS-232 port (factory-default Modbus *slave* — the recommended GenWatch path) and an RS-485 port (factory-default Modbus *master* — not directly usable until reconfigured). They are **not interchangeable** — RS-232 is ±5–12 V single-ended; RS-485 is differential 0–5 V. Wiring an RS-485 module to the RS-232 port (or vice versa) won't work.

### 2.1 Identify the RS-232 port on your H-100

The label varies by panel revision: "**RS-232**", "**GenLink**", "**PC**", or "**Service**". It is **not** the terminal block labeled "Mod-485" / "A B GND" — that's the RS-485 master port.

Physically it is either:
- A **DB9 male** connector (most common), or
- A **modular RJ-style** jack on newer revisions (the 0F7707 cable handles either)

If your H-100 has only an RS-485 terminal block visible and no DB9/RJ port, your panel revision may not have a populated RS-232 port — in that case use the [RS-485 path](#25-advanced-rs-485-instead-of-rs-232).

### 2.2 Recommended cabling — Generac 0F7707 + USB-DB9

```
   Raspberry Pi 5 USB ─── USB-DB9 adapter ─── Generac 0F7707 ─── H-100 RS-232 port
                          (FTDI chipset)       PC end (gray)        Panel end (black)
                          DB9 male             DB9 female
```

The 0F7707 cable is wired as a "null-modem" internally (TX↔RX crossover) and matches the panel's connector revision. **No additional null-modem adapter is needed when using 0F7707.**

### 2.3 Without the 0F7707 cable — DIY equivalent

If you can't source the 0F7707, the equivalent is:

```
   Pi USB ─── USB-DB9 (FTDI) ─── DB9 null-modem adapter ─── DB9-to-(panel) cable ─── H-100
              [DB9 male]         [DB9 F-F crossover]        [match the panel jack]
```

DB9 null-modem pinout (for reference — most off-the-shelf null-modem adapters already do this):

| PC side (DB9) | Panel side (DB9) |
|---|---|
| Pin 2 (RXD) | Pin 3 (TXD) |
| Pin 3 (TXD) | Pin 2 (RXD) |
| Pin 5 (GND) | Pin 5 (GND) |

Hardware handshake (RTS/CTS/DTR/DSR) is **not** required by the H-100 Modbus slave — only RX, TX, and GND are used.

### 2.4 Recommended H-100 panel settings (factory defaults — usually no change needed)

| Setting | Value |
|---|---|
| Modbus slave address | **100** (`0x64`) |
| Baud rate | **9600** |
| Data bits | **8** |
| Parity | **None** |
| Stop bits | **1** |
| Read function code | **3** (read holding registers) |

These are GenWatch's defaults too. If a previous integrator changed them on your panel, either restore them via GenLink or update `/etc/genwatch/config.yaml` to match what your panel is actually set to.

### 2.5 Advanced — RS-485 instead of RS-232

Use this only if you have a clear reason: cable run longer than ~15 m, multi-drop bus with other Modbus devices, or industrial noise that's interfering with the RS-232 link.

**Steps:**

1. **Reconfigure the H-100 RS-485 port from master to slave.** This is done via GenLink (Tools → Modbus → Port 2 → set to "Slave"). Note: this **disables the H-100's communication with any remote annunciators and HTS-485 transfer switches that were on that bus** — only do this if you've audited what else is on the RS-485 network.
2. **Wire to the RS-485 terminal block:**

   ```
      USB-RS485 adapter           H-100 RS-485 terminal block
      ────────────────            ──────────────────────────
      A  (D+ / TX+)      ──────►  A  (D+)
      B  (D− / TX−)      ──────►  B  (D−)
      GND (signal gnd)   ──────►  COM / GND
                                  │
                                  └── 120 Ω termination across A↔B at this end
      120 Ω at the adapter end ───┘ (usually a DIP switch on the module)
   ```

3. **Wiring rules:**
   - **A↔A, B↔B.** If the line is dead, swap A and B at one end. Half of all RS-485 problems are polarity swaps.
   - **Twisted pair only.** A and B must share the *same* twisted pair (e.g. blue / blue-white in Belden 9841). Don't use ribbon cable.
   - **Single shield ground.** Connect the shield/drain wire to GND at *one* end only (the Pi end) — grounding both ends creates a ground loop.
   - **120 Ω termination at both physical ends** of the bus, not in the middle.
   - **Linear bus, no spurs > 30 cm.** Don't tee off branches.
   - **Don't run alongside high-voltage.** Cross generator output cabling at 90° if you have to.
4. **Update GenWatch config** to match the RS-485 port's settings. The H-100 RS-485 port's factory default before reconfiguration is **4800 baud, 8N2** (not 9600 8N1). When you reconfigure it as a slave you can usually set it to 9600 8N1 to match GenWatch's defaults — set both ends to the same values:

   ```yaml
   serial:
     device: /dev/genwatch-modbus
     baud: 9600       # or whatever you set on the panel
     parity: N
     stopbits: 1
     bytesize: 8
   modbus:
     slave: 100       # whatever slave address the panel is set to
   ```

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

If you happen to be wiring the H-100 to the Pi's GPIO UART pins instead of using a USB adapter (advanced — most users should not do this), the Pi 5 needs the GPIO UART explicitly enabled. **For the standard USB-adapter setup documented here, skip this step.**

To enable GPIO UART:
```bash
sudo raspi-config nonint do_serial_hw 0      # enable hardware UART
sudo raspi-config nonint do_serial_cons 1    # disable login shell on it
sudo reboot
```
The on-board UART then appears as `/dev/ttyAMA10` on Pi 5 (different from Pi 4's `/dev/ttyAMA0`). Set `serial.device: /dev/ttyAMA10` in `config.yaml`.

---

## 4. Install GenWatch

Plug your USB-to-serial adapter (USB-DB9 for the recommended RS-232 path, or USB-RS485 for the advanced path) into one of the Pi's USB ports, then:

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
8. Installs the udev rule that symlinks any supported USB-to-serial adapter (RS-232 cable or RS-485 module) to `/dev/genwatch-modbus`.
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
[genwatch] Installing udev rule for /dev/genwatch-modbus
[genwatch] Running pre-flight diagnostics
== GenWatch doctor (v0.1.0) ==
  Python:    3.11.x
  Config:    /etc/genwatch/config.yaml
  Mock:      False
  Auth:      MISSING admin_password_hash — run: genwatch hash <password>
  Registers: /opt/genwatch/genwatch/registers/h100.yaml
             18 read + 4 write, slave=100
  Serial:    /dev/genwatch-modbus opens OK at 9600 8N1
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

A live Modbus link should respond within ~50 ms. If it doesn't, work through these in order — the first three catch the vast majority of cases:

| Check | Command / action |
|-------|------------------|
| **Wrong port** (RS-232 vs RS-485) | The H-100 RS-232 port is the factory Modbus slave. The RS-485 terminal block (labeled A/B/COM or Mod-485) is the master port and will *not* respond unless you've explicitly reconfigured it via GenLink. Use the RS-232 port (DB9 or RJ-style — see §2.1). |
| **Missing null-modem crossover** (RS-232) | If you're not using the Generac 0F7707 cable, you must have a DB9 null-modem adapter inline between the USB-DB9 cable and the panel. A straight-through cable will see silence — TX is talking to TX. |
| **Adapter plugged in and recognized?** | `lsusb` should list it. `ls -l /dev/genwatch-modbus` should show a symlink to `/dev/ttyUSB0` or similar. |
| Wrong baud rate or slave ID? | The H-100 RS-232 port's factory defaults are **9600 8N1, slave 100**. Verify on the panel via GenLink (Tools → Modbus). Mismatched baud = silence; mismatched slave ID = silence or exception code 11. |
| GND wire missing? | RS-232 needs a signal ground reference. If only TX and RX are connected the line floats. The 0F7707 cable handles this; DIY wiring must include GND (DB9 pin 5↔5). |
| Cable too long? | RS-232 max is ~15 m at 9600 baud. Beyond that, voltage levels degrade and you'll see intermittent silence. Use the RS-485 path (§2.5) for long runs. |
| Adapter or driver flaky? | Avoid no-name PL2303 clones — recent Linux kernels blacklist counterfeits. Replace with an FTDI-based adapter. |

**RS-485-specific (if you're on the RS-485 path):**

| Check | Command / action |
|-------|------------------|
| Polarity (A/B) swapped? | Most common RS-485 fault. Swap the two wires at the controller end and re-test. |
| No termination? | 120 Ω across A↔B at *both* ends, not the middle. Measure A↔B with the bus powered off — should read ~60 Ω (two 120 Ω in parallel). With no termination the bus looks like ~∞ Ω. |
| Adapter doesn't auto-direction? | Cheap RS-485 adapters need RTS to toggle TX/RX direction. Use a module with hardware auto-direction. |
| Conflicting Modbus master? | If you didn't reconfigure the H-100 RS-485 port from master to slave, the H-100 *is* the master and won't answer requests. If a Generac MLink is also on the bus you'll see collisions. |
| H-100 RS-485 still in master mode? | Open GenLink, Tools → Modbus → Port 2, confirm role is "Slave" and address is what GenWatch's config says. |

### Symptom: `Serial: CANNOT OPEN /dev/genwatch-modbus — Permission denied`

The genwatch user must be in the `dialout` group:

```bash
groups genwatch     # should include 'dialout'
# If not:
sudo usermod -aG dialout genwatch
sudo systemctl restart genwatch
```

### Symptom: `Serial: /dev/genwatch-modbus DOES NOT EXIST`

The udev rule didn't match your adapter. List what's there:

```bash
ls -l /dev/ttyUSB* /dev/ttyACM* 2>/dev/null
lsusb -v 2>/dev/null | grep -E "idVendor|idProduct|iProduct"
```

Either:
- Add your VID:PID to `/etc/udev/rules.d/99-genwatch-modbus.rules` and run `sudo udevadm control --reload-rules && sudo udevadm trigger`, OR
- Set `serial.device: /dev/ttyUSB0` (or whatever path `ls` showed) explicitly in `/etc/genwatch/config.yaml`, then `sudo systemctl restart genwatch`.

### Symptom: Service flapping (restarting every minute or so)

```bash
journalctl -u genwatch --since "5 minutes ago" --no-pager
```

The systemd unit has a watchdog set to 60 s; if the app's poller hangs (e.g. waiting forever on a serial read) the watchdog will SIGKILL and systemd restarts the service. The boot event log in the DB shows the boot pattern. If the underlying problem is the Modbus link going down and pymodbus blocking, run `sudo genwatch doctor` while the service is stopped to isolate.

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
                              │ Modbus RTU (RS-232 default; RS-485 advanced)
                              │ 9600 8N1, slave 100
                              v
                        ┌──────────────┐
                        │ H-100        │ Generac H-100 controller
                        │ controller   │ on the generator panel
                        └──────────────┘
```

### Reliability features

- **systemd watchdog**: `Type=notify`, `WatchdogSec=60s`. The app pings `sd_notify(WATCHDOG=1)` every 30 s while the poller loop is healthy. If the poller hangs (e.g. a pymodbus deadlock on a flaky link), systemd SIGKILLs and restarts within 60 s.
- **Refuse-to-start on Modbus failure**: in production the service exits cleanly with a clear error if it can't open the serial port. Operators see "service down", not a silent simulator.
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

The mock client simulates a plausible H-100 — engine state machine, electrical output, alarm injection. Control buttons drive the mock, so the full operator flow works without any hardware.

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
  udev/99-genwatch-modbus.rules Stable /dev/genwatch-modbus symlink
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
