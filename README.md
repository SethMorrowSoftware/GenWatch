# Castle Generator Monitor

Professional monitoring and control software for the **Generac H-100** industrial generator, running on a **Raspberry Pi 5**.

A single-pane operator console with live engine state, electrical output, two-step-confirm controls (start / stop / quiet-test / transfer), time-series history, alarms, and on-device configuration of the serial port, register map, and retention policy. Communicates with the H-100 controller over Modbus RTU.

> **Note on naming:** The product was previously called *GenWatch*. The internal Python package, systemd unit, CLI, and on-disk paths (`/etc/genwatch/`, `/dev/genwatch-modbus`, `genwatch.service`, the `genwatch` CLI) retain those identifiers to keep existing deployments and udev rules stable. Only the product name, UI, and operator-facing copy have been rebranded.

**Physical layer:** the H-100 has both an **RS-232** port (factory-default Modbus *slave*, 9600 8N1, address 100 — this is what GenLink uses, and what Castle Generator Monitor uses by default) and an **RS-485** port (factory-default Modbus *master* to remote annunciators and HTS transfer switches at 4800 8N2 — not directly usable until the panel is reconfigured).

**Three ways to get the Pi onto that link** — pick one based on what you already have:

| Path | When to pick it | What you need to buy |
|------|-----------------|----------------------|
| **A. Network serial bridge** (recommended) | You already have a Lantronix UDS/EDS/xDirect, Moxa NPort, Digi PortServer, or ser2net box wired to the H-100 (this is the common GenLink-over-the-network setup). | Nothing on the link side. Pi just needs Ethernet/Wi-Fi. See the [Quick start](#quick-start-existing-lantronix--network-serial-bridge) below. |
| **B. Direct USB-to-serial — RS-232** | No network bridge; Pi will live within ~15 m cable of the panel. | A USB-to-DB9 cable + the Generac 0F7707 (or a DIY null-modem equivalent — see [§2.3](#23-without-the-0f7707-cable--diy-equivalent)). Cost: $15–$150 depending on whether you buy the Generac part. |
| **C. Direct USB-to-RS485** | Long cable run (>15 m), or you're tapping an existing RS-485 SCADA bus, and you're willing to reconfigure the H-100's RS-485 port from master to slave via GenLink. | USB-RS485 adapter + twisted-pair cable + two 120 Ω terminators. ~$25. See [§2.5](#25-advanced-rs-485-instead-of-rs-232). |

If you already have option A in place, you can skip most of §1 and §2 — see the [Quick start](#quick-start-existing-lantronix--network-serial-bridge).

![architecture diagram — see docs/HARDWARE for wiring](#)

> **Reliability:** Test coverage lives under `backend/tests/` (register parsing/decoding, hardening, Slack notifier, and end-to-end mock flow). systemd hardened unit with sd_notify watchdog. Service refuses to start if the Modbus link is unreachable (no silent fallback to mock). Login rate-limited. SQLite WAL with crash-safe retention. Audit log on every control command.

---

## Table of contents

- [Quick start (existing Lantronix / network serial bridge)](#quick-start-existing-lantronix--network-serial-bridge)
1. [What you need (Bill of Materials)](#1-what-you-need-bill-of-materials)
2. [Wiring the Modbus link](#2-wiring-the-modbus-link)
3. [Prepare the Raspberry Pi 5](#3-prepare-the-raspberry-pi-5)
4. [Install Castle Generator Monitor](#4-install-castle-generator-monitor)
5. [Initial configuration](#5-initial-configuration)
6. [Verify the Modbus link](#6-verify-the-modbus-link)
7. [Operation](#7-operation)
8. [Security recommendations](#8-security-recommendations)
9. [Updating Castle Generator Monitor](#9-updating-castle-generator-monitor)
10. [Troubleshooting](#10-troubleshooting)
11. [Architecture overview](#11-architecture-overview)
12. [Adapting the register map](#12-adapting-the-register-map)
13. [Development](#13-development)
14. [License](#14-license)

---

## Quick start (existing Lantronix / network serial bridge)

If a Lantronix UDS/EDS/xDirect (or Moxa NPort, ser2net, etc.) is **already wired to the H-100 and already on your LAN** — for example, you've been using GenLink through it from a Windows PC via Com Port Redirector — this is the path. You're skipping the entire "buy and wire a USB serial cable" branch.

**What you need (much shorter list):**

| # | Item | Notes |
|---|------|-------|
| 1 | Raspberry Pi 5 (4 GB or 8 GB) | The host computer |
| 2 | Raspberry Pi 27 W USB-C PSU | 5 V / 5 A; cheap chargers cause brownouts |
| 3 | Active cooler for Pi 5 | Pi 5 throttles without active cooling |
| 4 | microSD card, 32 GB+ A2 class | Or NVMe + M.2 HAT for production |
| 5 | Pi 5 case | Argon NEO 5 BRED or similar |
| 6 | Ethernet drop (or 2.4/5 GHz Wi-Fi the Pi can reach) | Needs network access to the Lantronix |

No USB-to-DB9 adapter. No Generac 0F7707. No `dialout` group or udev rules. The Lantronix is already doing the serial work.

**Five steps:**

**1. Confirm the Lantronix is reachable from where the Pi will live.** From any machine on the same LAN (your laptop is fine):

```bash
ping -c 3 192.168.1.249              # use your Lantronix's actual IP
nc -vz 192.168.1.249 10001           # "succeeded" = it's listening
```

If `nc` says "succeeded" you're ready. If not, log into the Lantronix's web UI (`http://192.168.1.249`) and check Channel 1 → Connection → Connect Mode is set so it accepts incoming TCP (Active=None, Passive=Yes, Local Port 10001). See [§2.6](#26-network-serial-bridge-lantronix--moxa--ser2net) for the full Lantronix walkthrough if it isn't already configured.

> **Note on COM ports:** If you currently reach the Lantronix from Windows as a virtual COM port (COM8, etc.), that COM number is a Windows-only abstraction created by Lantronix's CPR driver — it doesn't apply on Linux. The Pi talks directly to TCP port 10001 (or whatever Local Port your Lantronix Channel 1 is set to). Nothing on the Windows side needs to change.

**2. Flash Raspberry Pi OS Bookworm (64-bit) onto the SD card** and bring up the Pi with SSH enabled. Full instructions in [§3](#3-prepare-the-raspberry-pi-5) — but if you've set up a Pi before, just use the Imager's "Edit Settings" panel to preconfigure user/SSH/Wi-Fi, then boot.

**3. Install Castle Generator Monitor** on the Pi:

```bash
ssh pi@<your-pi-ip>
git clone https://github.com/SethMorrowSoftware/GenWatch.git
cd GenWatch
sudo ./deploy/scripts/install.sh
```

The installer creates the `genwatch` system user, builds the UI, sets up the Python venv, drops a default config at `/etc/genwatch/config.yaml`, and installs the systemd unit. Full detail in [§4](#4-install-castle-generator-monitor).

**4. Set the admin password and the Lantronix host** in `/etc/genwatch/config.yaml`:

```bash
sudo genwatch hash 'pick-a-strong-password'        # prints a bcrypt hash
sudo nano /etc/genwatch/config.yaml
```

Replace `admin_password_hash: "REPLACE_ME"` with the hash you just generated, then confirm the link block points at your Lantronix:

```yaml
transport: tcp
modbus_tcp:
  host: 192.168.1.249    # ← your Lantronix's IP
  port: 10001            # ← Lantronix Channel 1 Local Port (default 10001)
  framer: rtu
```

Defaults already match `192.168.1.249:10001`; only edit if yours differs. Everything else in the file is fine as-is for an H-100 at factory address 100.

**5. Start it and verify:**

```bash
sudo systemctl restart genwatch
sudo genwatch doctor                                # full pre-flight
```

`doctor` should print `Modbus: slave 100 responded with [<value>]`. Open `http://<your-pi-ip>:8000` in a browser, log in, and the Live view should populate within ~2 seconds.

**If `doctor` reports `NO RESPONSE` on a socket that opens fine,** the most common cause is the Lantronix's "Pack Control" splitting Modbus frames. Log into the Lantronix web UI → Channel 1 → Connection → Pack Control → drop **Idle Gap Time** to ~10 ms (RTU's end-of-frame is a 3.5-character silence and the bridge needs to preserve it). Other causes covered in [§10 Troubleshooting](#10-troubleshooting).

**Power audit before you call it done:** the Lantronix needs to be on the **generator's** load side or its own UPS. If it's on a utility-side circuit, a power outage takes the bridge offline at exactly the moment you most want generator telemetry. Same goes for any network switch between the Pi and the bridge.

That's it. The rest of this README covers direct-cable installs (§1's full BOM, §2's wiring guides), how to set up the Lantronix from scratch (§2.6), production hardening (§8), and reference material.

---

## 1. What you need (Bill of Materials)

Approximate total: **$80–$120** if you're on Path A (network bridge already in place); **$150–$250** if you're wiring a new direct cable.

### Always required (every path)

| # | Item | Why | Recommended |
|---|------|-----|-------------|
| 1 | Raspberry Pi 5 (4 GB or 8 GB) | The host computer. 4 GB is plenty; 8 GB if you want headroom. | [Raspberry Pi 5 — 4 GB](https://www.raspberrypi.com/products/raspberry-pi-5/) |
| 2 | Raspberry Pi 27 W USB-C power supply | Pi 5 needs 5 V / 5 A. Cheap chargers cause brownouts and under-voltage warnings. | Official Raspberry Pi 27 W PSU |
| 3 | Active cooler for Pi 5 | Pi 5 throttles aggressively without active cooling, especially in an outdoor/cabinet enclosure. | Official Raspberry Pi 5 Active Cooler |
| 4 | microSD card, 32 GB+, A2 class | OS + database storage. A2 cards have markedly better random-write IOPS — important for SQLite. | SanDisk Extreme Pro 64 GB A2, or Samsung Pro Endurance 64 GB |
| 5 | Pi 5 case with cooling cut-outs | Mechanical protection. Argon NEO 5 BRED, Pironman 5, or a sealed DIN-rail enclosure for industrial install. | Argon NEO 5 BRED (active-cooler compatible) |

### Path A — Network serial bridge (recommended)

You already have (or are willing to install) a Lantronix UDS/EDS/xDirect, Moxa NPort, Digi PortServer, or ser2net box wired to the H-100. The Pi connects to it over the LAN.

| # | Item | Why |
|---|------|-----|
| A1 | Ethernet drop at the Pi (or Wi-Fi the Pi can reach) | The only "link" the Pi needs. No serial hardware on the Pi side at all. |

That's the entire path-A link BOM. **You do not need the Generac 0F7707 or a USB-to-DB9 adapter** — those live at the Lantronix end, and if the Lantronix is already wired to the H-100 they're already taken care of. If you're installing a new Lantronix from scratch, the bridge-end wiring is covered in [§2.6](#26-network-serial-bridge-lantronix--moxa--ser2net) (and you'll typically reuse a 0F7707 or DIY null-modem there).

### Path B — Direct RS-232 USB-to-serial

Use this if the Pi will sit within ~15 m of the panel and you don't want a network bridge in the critical path.

| # | Item | Why | Recommended |
|---|------|-----|-------------|
| B1 | **USB-to-DB9 serial adapter** with a quality chipset | Bridges the Pi's USB to a DB9 cable. Avoid no-name PL2303 clones — they're driver-unstable on modern Linux. | StarTech **ICUSB232V2** (FTDI), Tripp Lite **USA-19HS** (Keyspan), or any FTDI-FT232R-based USB-DB9 cable. ~$25. |
| B2a | **Generac 0F7707 PC interface cable** *(easiest)* | The factory service cable. Gray "Computer" end on the PC side, Black "Control Panel" end on the H-100 RS-232 port. Handles the panel-side connector + null-modem crossover for you. Supported, no wiring research. | Generac part **0F7707** (Generac dealers; $80–$150). |
| B2b | **— OR — DIY null-modem equivalent** *(cheaper)* | Functionally identical to the 0F7707 if your H-100 has the DB9-style PC port (most revisions). See [§2.3](#23-without-the-0f7707-cable--diy-equivalent). | DB9 male-male straight cable + DB9 F-F null-modem adapter (Monoprice/Amazon, ~$10 total). For RJ-style panel jacks, you'll need to crimp an RJ-to-DB9 adapter per the H-100 service manual pinout. |

### Path C — Direct RS-485

Long cable run (>15 m), or you need to drop the Pi onto an existing RS-485 SCADA bus. Requires reconfiguring the H-100's RS-485 port from "master" to "slave" via GenLink and disconnecting any annunciators / HTS-485 peripherals from that port.

| # | Item | Why | Recommended |
|---|------|-----|-------------|
| C1 | **USB-to-RS485 adapter** with hardware auto-direction and a quality chipset | Bridges the Pi's USB to the H-100 RS-485 terminal block (A/B/GND). | FTDI **USB-RS485-WE-1800-BT**, DSD TECH **SH-U10** (CH340), Waveshare **USB to RS485** (FT232) |
| C2 | Twisted-pair shielded cable, 22-24 AWG | The RS-485 differential pair plus shield/drain. Up to ~1000 m at 9600 baud, ~300 m typical with consumer cable. | Belden **9841** (one twisted pair + shield) |
| C3 | Two 120 Ω 1/4 W resistors | Bus termination at both physical ends of the linear bus. Many USB-RS485 adapters have a built-in terminator switchable by a DIP — check before buying extras. | Standard 1/4 W, ±5 % carbon-film |

### Optional but recommended

| Item | Why |
|------|-----|
| **NVMe SSD + Pi 5 M.2 HAT** (PCIe HAT + 256 GB+ NVMe) | Much faster + more durable than microSD. SQLite + journal rotates cleanly. Reduces SD-wear failures over multi-year deployments. |
| **UPS HAT** (Waveshare UPS HAT (E) or PiSugar) | Survives utility-side outages without filesystem corruption. Especially relevant since the Pi is monitoring a *generator* — utility loss is the interesting event. |
| **Touchscreen** (Official Pi 7" Touch Display 2) | Wall-mounted in the generator room as a HMI. The UI is responsive down to 1024 × 600. |
| **Tailscale subscription** (free for ≤ 3 users) | Secure remote access without exposing the Pi to the public internet. See [§8 Security](#8-security-recommendations). |
| **DIN-rail mount** | For control-panel installation. |
| **Wago lever-nut connectors** (221-412) | Clean, screwless wiring at the controller terminal. |

### Compatible USB-to-serial chipsets (paths B and C only — skip if you're on path A)

The bundled udev rule symlinks any of these adapters to `/dev/genwatch-modbus` (covers both RS-232 cables and RS-485 modules — they use the same USB-to-serial bridge chips):

- **FTDI FT232 / FT231X / FT232H** (USB VID 0x0403) — most reliable, the safe choice for mission-critical sites
- **Silicon Labs CP2102 / CP2104** (VID 0x10C4) — good middle ground, well-supported on Linux
- **WCH CH340 / CH341** (VID 0x1A86) — cheap, generally works fine on Bookworm; budget-friendly
- **Prolific PL2303** (VID 0x067B) — works in principle, but **avoid no-name clones** — many ship with counterfeit chips that get blacklisted by recent Linux kernels. If you can't tell, get FTDI instead.

For a field-deployed monitoring station that's expected to run for years, **buy an FTDI-based adapter.** The ~$15 premium is paid back the first time you don't have to drive out to a generator pad because a Prolific clone got blacklisted by a kernel update.

---

## 2. Wiring the Modbus link

**Path A (Lantronix already wired) — skip this entire section.** Jump to [§3 Prepare the Pi](#3-prepare-the-raspberry-pi-5). The serial wiring is between the Lantronix and the H-100; you've already got it. The [Quick start](#quick-start-existing-lantronix--network-serial-bridge) at the top covers everything you need.

**Path A (installing a new Lantronix from scratch)** — see [§2.6](#26-network-serial-bridge-lantronix--moxa--ser2net) for the bridge-end wiring and Lantronix web-UI walkthrough.

**Paths B and C (direct cable from Pi to panel)** — §2.1–§2.5 below.

The H-100 has both an RS-232 port (factory-default Modbus *slave* — the recommended path for a direct cable) and an RS-485 port (factory-default Modbus *master* — not directly usable until reconfigured). They are **not interchangeable** — RS-232 is ±5–12 V single-ended; RS-485 is differential 0–5 V. Wiring an RS-485 module to the RS-232 port (or vice versa) won't work.

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

These are Castle Generator Monitor's defaults too. If a previous integrator changed them on your panel, either restore them via GenLink or update `/etc/genwatch/config.yaml` to match what your panel is actually set to.

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
4. **Update Castle Generator Monitor config** to match the RS-485 port's settings. The H-100 RS-485 port's factory default before reconfiguration is **4800 baud, 8N2** (not 9600 8N1). When you reconfigure it as a slave you can usually set it to 9600 8N1 to match the monitor's defaults — set both ends to the same values:

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

### 2.6 Network serial bridge (Lantronix / Moxa / ser2net)

If a Lantronix UDS/EDS/xDirect (or Moxa NPort, Digi PortServer, a second Pi running ser2net, etc.) is already wired to the H-100's serial port and exposed on your LAN, you can skip the USB-to-serial cable on this Pi entirely. The bridge tunnels raw bytes between a TCP socket and the physical RS-232/RS-485 line; the H-100 still frames Modbus **RTU** on the wire — so this is *not* Modbus/TCP (which uses a different frame and port 502).

**What you keep / drop from the BOM:**
- **Drop** the USB-to-DB9 adapter (#5b). The Pi only needs Ethernet/Wi-Fi.
- **Keep** the **Generac 0F7707** (or the DIY null-modem + panel adapter from §2.3). The bridge has a standard DB9; the H-100's panel connector and required null-modem crossover still need to be handled. The gray "PC" end of the 0F7707 plugs into the bridge's DB9.
- **Add** the bridge itself (e.g. Lantronix UDS-1100 ≈ $200) and an Ethernet drop at the generator pad.

**Power matters.** The bridge needs to be on the **generator's** load side or its own UPS — otherwise during a utility outage it dies and GenWatch goes blind exactly when you most want telemetry. Same applies to any network switch between the Pi and the bridge.

**Lantronix configuration (web UI on `http://<bridge-ip>`):**

1. **Serial settings** (Channel 1 → Serial Settings) — match the H-100: **9600 baud, 8 data bits, No parity, 1 stop bit, Flow control: None**. If you're on the RS-485 path with factory-default panel settings, use 4800 8N2 instead.
2. **Connect Mode** (Channel 1 → Connection) — **Active Connection: None**, **Passive Connection: Yes**, **Local Port: 10001** (the Lantronix default). This makes the bridge listen for incoming TCP and forward bytes to the serial port.
3. **Packing** (Channel 1 → Connection → Pack Control) — set **Idle Gap Time** low (≈ 10 ms). Modbus RTU's end-of-frame is a 3.5-character silence; aggressive packing/Nagle will split frames mid-packet and break framing. Disable Send Frame Immediate only if you observe problems.
4. **Security** — change the **enable password** under Setup → Security, and (if your firmware supports it) disable Telnet config on **port 9999** from the WAN side. Lantronix devices historically ship with no password and an open config port; treat the bridge like any other admin-accessible network device.

**GenWatch config** (`/etc/genwatch/config.yaml`):

```yaml
transport: tcp
modbus_tcp:
  host: 192.168.1.249   # your Lantronix's IP or hostname
  port: 10001           # Channel 1 Local Port from step 2
  timeout_s: 1.5        # bump to 3-5s if you see "timeout" in /api/comms
  connect_timeout_s: 3.0
  framer: rtu
```

After editing, restart: `sudo systemctl restart genwatch`. The Settings UI also lets you flip transport between **TCP bridge** and **USB serial** under Settings → Modbus Link; a service restart is still required after saving.

**Verifying the link** before starting GenWatch:

```bash
# Confirm the Lantronix is reachable
ping -c 3 192.168.1.249

# Confirm the raw-TCP port is open
nc -vz 192.168.1.249 10001     # → "succeeded" means listening

# Optional: poll one Modbus register through the bridge (requires the mbpoll tool)
mbpoll -m rtu -a 100 -r 1 -c 1 -t 4:hex -P none 192.168.1.249:10001
```

If `nc` fails, check the bridge's Connect Mode (step 2 above) and any firewall between the Pi and the bridge. If `nc` succeeds but Modbus reads time out, the most common cause is packing settings (step 3) splitting frames — drop Idle Gap Time and try again.

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
   - Username + password (this is the Pi's *Linux* login, distinct from the Castle Generator Monitor operator login)
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

## 4. Install Castle Generator Monitor

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
== Castle Generator Monitor — doctor (v0.1.0) ==
  Python:    3.11.x
  Config:    /etc/genwatch/config.yaml
  Mock:      False
  Auth:      MISSING admin_password_hash — run: genwatch hash <password>
  Registers: /opt/genwatch/genwatch/registers/h100.yaml
             35 read + 5 write, slave=100
  Serial:    /dev/genwatch-modbus opens OK at 9600 8N1
  Modbus:    slave 100 responded with [0] (37ms)

⚠  ADMIN PASSWORD NOT SET
```

If you see `Modbus: NO RESPONSE`, jump to [§10 Troubleshooting](#10-troubleshooting). The installer continues regardless — the service just won't start until the admin password is set.

---

## 5. Initial configuration

### 5.1 Set the admin password and confirm the link target

```bash
sudo genwatch hash 'pick-a-strong-password'
# → $2b$12$XJZ... (paste this whole line)
sudo nano /etc/genwatch/config.yaml
```

Do three things in the editor:

1. Replace `admin_password_hash: "REPLACE_ME"` with the hash you just generated.
2. Confirm the **transport block** matches your setup:
   - **Path A (Lantronix / network bridge):** `transport: tcp` with `modbus_tcp.host` pointing at your bridge's IP (default is `192.168.1.249:10001` — change if yours differs).
   - **Paths B/C (direct USB serial):** `transport: serial` and confirm `serial.device` matches what got assigned (the installer prints what it saw; usually `/dev/genwatch-modbus`).
3. Save and exit.

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

# Read a sweep of 16 registers starting at 0x0080 (status bitfield region)
sudo -u genwatch genwatch modbusdump --addr 0x0080 --count 16
# → 0x0080  0x8000  (input_status_1 — bit 0x8000 = "Switch In Auto")
# → 0x0082  0x0100  (output_status_1 — bit 0x0100 = "Stopped")
# → 0x0083  0x0000  (output_status_2 — no oil/coolant alarms)
# → ...

# Try the kW register specifically (H-100 default 0x00AE, u32 / 2 regs)
sudo -u genwatch genwatch modbusdump --addr 0x00AE --count 2

# Frequency (scale 0.1 — raw 600 = 60.0 Hz)
sudo -u genwatch genwatch modbusdump --addr 0x00B2 --count 2
```

If `modbusdump` returns values but they don't match what you see on the H-100 panel, you may have a G-Panel revision (addresses shift by 6–0x20) or a dealer-customized firmware. See [§12 Adapting the register map](#12-adapting-the-register-map).

---

## 7. Operation

### Daily use

The Live view is the operator console: engine state, electrical output, control buttons, recent events.

- **Remote Start**: only enabled when state is `stopped`. Two-step confirm with an 8-char hex token that expires in 30 s.
- **Remote Stop**: enabled while running/exercising. Initiates the controller's normal cool-down cycle.
- **Quiet-Test**: 30-minute unloaded exercise. Idle exercise schedule shown at the top right.
- **Transfer back**: while running, hand the load back to utility and cool down.

All commands are FC16 multi-register writes:
- **Start / Stop / Transfer** write a 3-register payload to `0x019C` (`START_BITS`) — e.g. start = `[0x0080, 0x0000, 0x0000]`, stop = `[0x0000, 0x0000, 0x0000]`, transfer = `[0x0080, 0x0000, 0x0080]`.
- **Quiet-Test** writes `0x0001` to `0x022B` (`QUIETTEST_STATUS`); the same register reads back the test's running status.
- **Acknowledge Alarm** writes `0x0001` to `0x012E` (`ALARM_ACK`).

Every command is audit-logged with the operator, timestamp, register, the actual word values written, and the result.

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

Castle Generator Monitor is designed for a **trusted LAN** deployment. By default it listens on `0.0.0.0:8000` over plain HTTP; cookies are not `Secure`. This is appropriate for a Pi sitting in the same building as the generator on a private network. Do not expose port 8000 to the public internet without the following:

### 8.1 Use Tailscale for remote access

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

Tailscale gives you an encrypted private mesh; the Pi gets an IP like `100.x.y.z` reachable only from your other Tailscale devices. Combined with [Tailscale ACLs](https://tailscale.com/kb/1018/acls) this is more than sufficient for most field deployments. You can also enable HTTPS via Tailscale's `tailscale cert` if you want browser-trusted TLS.

### 8.2 Or terminate TLS with Caddy in front of the monitor

```bash
sudo apt-get install -y caddy
sudo tee /etc/caddy/Caddyfile <<EOF
genwatch.your-domain.example {
    reverse_proxy localhost:8000
}
EOF
sudo systemctl restart caddy
```

Caddy will auto-fetch a Let's Encrypt cert if the hostname resolves publicly. Then change the cookie to `secure=True` (line 30 in `backend/genwatch/api/auth.py`).

### 8.3 Firewall

```bash
sudo apt-get install -y ufw
sudo ufw allow ssh
sudo ufw allow from 192.168.0.0/16 to any port 8000
sudo ufw enable
```

Restricts the monitor's port to your LAN ranges.

### 8.4 Built-in defenses

- **Login rate-limiter**: 5 attempts then 1 attempt per 3 minutes per source IP. State resets on service restart.
- **JWT secret regeneration**: invalidate all sessions by regenerating: `sudo genwatch gensecret` → paste into config.yaml `jwt_secret:` → `sudo systemctl restart genwatch`.
- **Audit log** in `/var/lib/genwatch/db.sqlite` table `audit` records every login attempt, confirm token issue/use, and control command with the source IP, operator, and result.
- **Server-side state validity**: even if the UI bug-allows clicking "Start" while the engine is running, the server rejects with HTTP 409 and audit-logs the denial.

---

## 9. Updating Castle Generator Monitor

For production updates, use this sequence so you don't lose observability:

```bash
cd ~/genwatch
git pull
sudo deploy/scripts/install.sh
sudo systemctl restart genwatch
sudo systemctl status genwatch --no-pager
sudo genwatch doctor
```

If you changed the register map, immediately run verification after login:

```bash
curl -b cookies.txt -X POST http://localhost:8000/api/registers/reload
curl -b cookies.txt http://localhost:8000/api/registers/verify
```

Keep a dated backup of `/etc/genwatch/config.yaml` before major upgrades.

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

### Symptom: `TCP: CANNOT REACH <host>:<port>` (path A — Lantronix / network bridge)

The Pi can't even open a TCP connection to the bridge — this is a network or bridge-config problem, not a Modbus problem.

| Check | Command / action |
|-------|------------------|
| **Bridge powered and on the LAN?** | `ping <bridge-ip>` from the Pi. No reply = network or power issue. Check that the Lantronix is plugged into a powered switch port and that its status LED is solid. |
| **Bridge listening on the port?** | `nc -vz <bridge-ip> <port>` (typically `10001`). "Connection refused" means the bridge is up but not listening on that port. Log into the Lantronix web UI → Channel 1 → Connection → confirm **Active Connect = None, Passive Connect = Yes, Local Port = 10001** (or whatever you configured). |
| **Wrong port number?** | Multi-port Lantronix devices use 10001 for port 1, 10002 for port 2, etc. Single-port devices always use 10001. Check the bridge's web UI for the actual Local Port of the channel wired to the H-100. |
| **Firewall between Pi and bridge?** | If they're on different VLANs/subnets, a router ACL may be blocking TCP/10001. `traceroute <bridge-ip>` shows the path. Open the port on the relevant firewall, or move them onto the same subnet. |
| **Other client holding the socket?** | Some Lantronix configurations only allow one client at a time. If a Windows machine is using CPR to hold COM8 → 10001 open, the Pi may get refused. Close the CPR session (or set the Lantronix to allow multiple connections under Channel 1 → Connection → Endpoint Configuration). |

### Symptom: `Modbus: NO RESPONSE` but TCP socket connects fine (path A)

Bytes are flowing but the H-100 isn't replying — or its reply is getting mangled. The TCP layer is fine; this is a serial-side or framing problem at the bridge.

| Check | Command / action |
|-------|------------------|
| **Pack Control splitting RTU frames** *(by far the most common)* | Modbus RTU's end-of-frame is a 3.5-character silence. If the Lantronix's Pack Control is set aggressively, it'll forward bytes mid-frame and the H-100 sees malformed packets. Lantronix web UI → Channel 1 → Connection → **Pack Control → Idle Gap Time** → set to **~10 ms**. (On older Lantronix firmware, equivalent setting may be labeled "Send Characters" or "Force Transmit".) |
| Bridge serial settings don't match the H-100 | Lantronix web UI → Channel 1 → Serial Settings. Must be **9600 baud, 8 data bits, No parity, 1 stop bit, Flow control: None** for the H-100 RS-232 port. (If the bridge is wired to the H-100 RS-485 port and the panel is at factory defaults, it'd be 4800 8N2 — but that won't respond at all because the RS-485 port is master by default.) |
| Bridge wired to the wrong panel port | The Lantronix should be wired to the H-100's RS-232 PC port via the 0F7707 (or DIY null-modem equivalent). If it's on the RS-485 terminal block without panel reconfig, the H-100 won't answer (RS-485 is master by factory default). |
| Wrong slave ID in GenWatch config | H-100 default is 100 (0x64). Check `modbus.slave:` in `/etc/genwatch/config.yaml`. |
| Latency > timeout | LAN-attached bridges add 5–20 ms per request; congested Wi-Fi can blow past 1.5 s. Bump `modbus_tcp.timeout_s` to 3–5 s in `/etc/genwatch/config.yaml` and restart. |

### Symptom: `Modbus: NO RESPONSE` in `genwatch doctor` / "Comms lost" in UI (paths B & C — direct cable)

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
| H-100 RS-485 still in master mode? | Open GenLink, Tools → Modbus → Port 2, confirm role is "Slave" and address is what the monitor's config says. |

### Symptom: `Serial: CANNOT OPEN /dev/genwatch-modbus — Permission denied` (paths B & C)

The genwatch user must be in the `dialout` group:

```bash
groups genwatch     # should include 'dialout'
# If not:
sudo usermod -aG dialout genwatch
sudo systemctl restart genwatch
```

### Symptom: `Serial: /dev/genwatch-modbus DOES NOT EXIST` (paths B & C)

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
│   • prime (1.5 s): output_status_1..8 bitfields, key switch,     │
│                    quiet-test status, alarm count                │
│   • base  (15 s):  RPM, V, A, Hz, kW, oil P/T, coolant, batt,…  │
│  Engine state + active alarms derived from the bitfield bits.    │
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

The shipped `backend/genwatch/registers/h100.yaml` is derived from the [`jgyates/genmon`](https://github.com/jgyates/genmon/blob/master/genmonlib/generac_HPanel.py) project's `generac_HPanel.py` — a field-tested open-source H-100 integration cross-checked against the [Monico H100 Combined Data Map](https://www.monicoinc.com/downloads/H100_Combined_Data_Map-WEB.xls). If you're seeing wrong values on a real panel, the most likely causes (in order) are:

1. **It's actually a G-Panel, not an H-100.** Generac's industrial line includes a G-Panel sibling controller. Addresses shift by 6–0x20 — see `GPanelReg` in genmon's source. Symptom: the telemetry block reads as garbage but the link is healthy.
2. **Dealer-customized firmware** with different addresses for a few sensors.
3. **A scale factor difference** — values look 10× or 100× off but otherwise correct.

To investigate:

```bash
# Sweep the status bitfield region (state, alarms, key switch)
sudo -u genwatch genwatch modbusdump --addr 0x0080 --count 16

# Sweep the telemetry block (engine + AC output)
sudo -u genwatch genwatch modbusdump --addr 0x008A --count 48

# Probe common H-100 register regions
for a in 0x0080 0x008A 0x009E 0x00AE 0x012F 0x0130 0x019C 0x022B; do
  sudo -u genwatch genwatch modbusdump --addr $a --count 4
done
```

Cross-reference the values you see with what the H-100 panel shows on its own screen. When you have the right addresses, edit:

```bash
sudo nano /opt/genwatch/genwatch/registers/h100.yaml
```

Then hot-reload (admin auth required):

```bash
curl -b cookies.txt -X POST http://localhost:8000/api/registers/reload

# Run automated verification (static safety + live read probe)
curl -b cookies.txt http://localhost:8000/api/registers/verify
```

`/api/registers/verify` is read-only. It reports:
- **static**: map structure/safety issues (overlaps, invalid FC, invalid tier, etc.)
- **live**: per-register Modbus read failures against the currently configured H-100 link

This makes commissioning easier: edit YAML → reload → verify → only then enable operator controls.

Or restart the service to fully rebind the poller batching:

```bash
sudo systemctl restart genwatch
```

The YAML schema is documented in comments at the top of `h100.yaml`. Key sections:

- **`registers`** — per register: `addr`, `fc` (3/4), `type` (`u16`/`s16`/`u32`/`s32`/`bitfld`/`enum`), `scale`, `tier` (`prime`/`base`), `group`, `unit`, `warn_range`, `alarm_range`. Most H-100 telemetry slots are 2-register `u32` blocks; the meaningful value lives in the low word and the decoder reads them as big-endian.
- **`engine_state_bits`** — priority-ordered rules mapping bitfield bits to engine states (`stopped` / `cranking` / `running` / `cooling` / `exercising` / `alarm`). First matching rule wins.
- **`alarm_bits`** — flat table of alarm bits across `output_status_2..8`. Each entry has `register`, `mask`, `code`, `desc`, `severity` (`alarm`/`warn`). Multiple alarms can be active simultaneously.
- **`controls`** — write-gated commands. Single-register writes use `value: N` with `fc: 6`; multi-register writes (H-100 start/stop/transfer at `0x019C`) use `values: [w1, w2, w3]` with `fc: 16`.

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
# Optional (recommended for local checks):
# .venv/bin/pip install -r requirements-dev.txt
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
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests/ -v
# Test categories: register decode + batching, e2e mock control flow,
# rate-limit, events retention, sd_notify, refuse-to-start safety, Slack notifier
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
| GET    | `/api/registers/verify`       | Static + live register verification (admin) |
| WS     | `/ws/live`                    | `snapshot` / `transition` / `alarm` |

All errors return JSON `{ detail: { code, message } }` with appropriate HTTP status.

---

## 14. License

MIT — see [LICENSE](LICENSE).
