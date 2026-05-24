# Castle Generator Monitor

Professional monitoring and control software for the **Generac H-100** industrial generator, running on a **Raspberry Pi 5** and talking to the controller over a **Modbus-RTU-over-TCP** network bridge (Lantronix UDS / EDS / xDirect, Moxa NPort, Digi PortServer, ser2net, etc.).

A single-pane operator console: live engine state, electrical output, two-step-confirm controls (start / stop / quiet-test / transfer), time-series history, alarms, and on-device configuration of the link, register map, and retention policy.

> **Note on naming.** The product was previously called *GenWatch*. The internal Python package, systemd unit, CLI, and on-disk paths (`/etc/genwatch/`, `genwatch.service`, the `genwatch` CLI) keep those identifiers so existing deployments don't break. Only the operator-facing copy was rebranded.

> **Reliability summary.** Hardware watchdog on pid 1 (Pi reboots on kernel hang); software watchdog on the polling loop driven by a monotonic prime-poll heartbeat (service restarts on a deadlocked read); TCP keepalive on the Modbus socket (dead Lantronix detected in ~60 s); SQLite WAL with `synchronous=FULL` (audit/alarm rows survive a power cut); graceful degradation when the link is down (UI stays reachable, comms shown as LOST, reconnect in the background); login rate-limited; audit log on every control command. Test coverage under `backend/tests/`.

---

## Table of contents

- [Quick start](#quick-start)
- [1. Prerequisites](#1-prerequisites)
- [2. Prepare the Raspberry Pi 5](#2-prepare-the-raspberry-pi-5)
- [3. Configure the network bridge](#3-configure-the-network-bridge)
- [4. Install Castle Generator Monitor](#4-install-castle-generator-monitor)
- [5. Initial configuration](#5-initial-configuration)
- [6. Verify the Modbus link](#6-verify-the-modbus-link)
- [7. Operation](#7-operation)
- [8. Security recommendations](#8-security-recommendations)
- [9. Updating](#9-updating)
- [10. Troubleshooting](#10-troubleshooting)
- [11. Architecture overview](#11-architecture-overview)
- [12. Adapting the register map](#12-adapting-the-register-map)
- [13. Development](#13-development)
- [14. License](#14-license)

---

## Quick start

Assumes a Raspberry Pi 5 running Raspberry Pi OS Bookworm (64-bit) and a network serial bridge (Lantronix, Moxa, etc.) that is **already wired to the H-100 and already on your LAN** — e.g. the same bridge you've been using with GenLink from Windows.

```bash
# 1. Verify the bridge is reachable from where the Pi will live
ping -c 3 192.168.1.249              # your bridge's IP
nc -vz 192.168.1.249 10001           # "succeeded" = listening

# 2. SSH to the Pi and install
ssh pi@<your-pi-ip>
git clone https://github.com/SethMorrowSoftware/GenWatch.git
cd GenWatch
sudo ./deploy/scripts/install.sh

# 3. Set the admin password and point at the bridge
sudo genwatch hash 'pick-a-strong-password'   # prints a bcrypt hash
sudo nano /etc/genwatch/config.yaml           # paste the hash, set modbus_tcp.host

# 4. Start it and verify
sudo systemctl restart genwatch
sudo genwatch doctor                          # expect "Modbus: slave 100 responded"
```

Then open `http://<your-pi-ip>:8000` and log in. The Live view should populate within ~2 s.

If `genwatch doctor` reports `NO RESPONSE` on a TCP socket that opens fine, the most common cause is the bridge's **Pack Control** splitting Modbus RTU frames — see [§10 Troubleshooting](#10-troubleshooting). If `nc` itself fails, the bridge isn't reachable or isn't listening — see [§3](#3-configure-the-network-bridge).

---

## 1. Prerequisites

You need:

| # | Item | Notes |
|---|------|-------|
| 1 | Raspberry Pi 5 (4 GB or 8 GB) | The host computer. 4 GB is plenty. |
| 2 | Raspberry Pi 27 W USB-C PSU | 5 V / 5 A. Cheap chargers cause brownouts and under-voltage warnings. |
| 3 | Active cooler for Pi 5 | Pi 5 throttles aggressively without active cooling, especially in a cabinet. |
| 4 | microSD card, 32 GB+ A2 class | Or NVMe + Pi 5 M.2 HAT for longer life. |
| 5 | Pi 5 case | Argon NEO 5 BRED, or a sealed DIN-rail enclosure for industrial install. |
| 6 | Ethernet drop the Pi can reach the bridge from | Or 2.4/5 GHz Wi-Fi. **Same broadcast domain as the bridge is simplest;** different VLANs need an ACL hole for TCP/10001 (or whatever port your bridge uses). |
| 7 | Network serial bridge already wired to the H-100 | Lantronix UDS/EDS/xDirect, Moxa NPort, Digi PortServer, a second Pi running ser2net, etc. If you've been reaching the H-100 from Windows via Lantronix's CPR driver, this is what you already have. |

**Power audit.** The bridge must be on the **generator's** load side or its own UPS. Otherwise a utility outage takes the bridge offline at exactly the moment you most want generator telemetry. Same goes for any network switch between the Pi and the bridge.

**Optional but recommended:**

- **NVMe SSD + Pi 5 M.2 HAT** (PCIe HAT + 256 GB+ NVMe). Faster + much more durable than microSD for a write-heavy SQLite workload.
- **UPS HAT** (Waveshare UPS HAT (E), PiSugar, or a small DIN-rail UPS feeding the Pi's PSU). Survives utility-side outages without filesystem corruption. The service ships with `synchronous=FULL` SQLite so a power cut won't corrupt the DB, but the OS root partition still wants a clean shutdown.
- **Touchscreen** (Official Pi 7" Touch Display 2). Wall-mounted in the generator room as a HMI. The UI is responsive down to 1024 × 600.
- **Tailscale** (free for ≤ 3 users). Secure remote access without exposing port 8000 to the internet. See [§8 Security](#8-security-recommendations).

---

## 2. Prepare the Raspberry Pi 5

### 2.1 Install Raspberry Pi OS Bookworm (64-bit)

1. Download [Raspberry Pi Imager](https://www.raspberrypi.com/software/) on a desktop computer.
2. Insert the microSD (or your NVMe via a USB adapter).
3. Choose:
   - Device: **Raspberry Pi 5**
   - OS: **Raspberry Pi OS (64-bit)** → *Raspberry Pi OS Lite (64-bit)* recommended (no desktop needed). Standard works too.
4. Click the gear icon → **OS customization** and set:
   - Hostname: `genwatch` (so it's reachable at `genwatch.local` via mDNS)
   - Username + password (this is the Pi's *Linux* login, distinct from the Castle Generator Monitor operator login)
   - SSID/password for Wi-Fi (or skip if using Ethernet)
   - Enable SSH with password authentication
   - Locale, timezone
5. **Write** the image.

### 2.2 First boot

Power up the Pi and SSH in:

```bash
ssh <user>@genwatch.local
```

Run system updates:

```bash
sudo apt-get update && sudo apt-get -y upgrade
sudo reboot
```

---

## 3. Configure the network bridge

This section is the bridge-end setup. If your bridge is already in service (e.g. you've been using GenLink through it from Windows) and `nc -vz <bridge-ip> 10001` from any LAN machine reports `succeeded`, you can skip ahead to [§4](#4-install-castle-generator-monitor).

The bridge tunnels raw bytes between a TCP socket and the physical RS-232 line wired to the H-100. The H-100 still frames Modbus **RTU** on the wire — so this is *not* Modbus/TCP (different frame, port 502). Castle Generator Monitor handles the RTU framing on the Pi side.

### 3.1 Lantronix (UDS / EDS / xDirect)

Open the bridge's web UI at `http://<bridge-ip>` and:

1. **Serial settings** — Channel 1 → Serial Settings — match the H-100's RS-232 port: **9600 baud, 8 data bits, No parity, 1 stop bit, Flow control: None**. These are the H-100 factory defaults.
2. **Connect Mode** — Channel 1 → Connection — **Active Connection: None**, **Passive Connection: Yes**, **Local Port: 10001** (the Lantronix default). This makes the bridge listen for incoming TCP and forward bytes to the serial port.
3. **Packing** — Channel 1 → Connection → **Pack Control → Idle Gap Time** → set to **~10 ms**. Modbus RTU's end-of-frame is a 3.5-character silence; aggressive packing splits frames mid-packet and breaks framing. This is the single most common cause of "TCP connects but Modbus times out".
4. **Security** — Setup → Security — change the **enable password** and, if your firmware supports it, disable the Telnet config port (9999) from the WAN side. Lantronix devices historically ship with no password.

### 3.2 Moxa NPort, Digi PortServer, ser2net

The settings are equivalent: passive TCP listener on a local port, 9600 8N1, low inter-character timeout / disabled Nagle. Consult your vendor's docs for the exact menu names.

### 3.3 Verify the bridge before installing the Pi software

From any machine on the same LAN:

```bash
ping -c 3 192.168.1.249              # bridge's IP
nc -vz 192.168.1.249 10001           # "succeeded" = listening on TCP

# Optional: poll one Modbus register through the bridge (requires mbpoll)
mbpoll -m rtu -a 100 -r 1 -c 1 -t 4:hex -P none 192.168.1.249:10001
```

If `nc` fails, re-check step 2 above and any firewall between you and the bridge. If `nc` succeeds but `mbpoll` times out, re-check step 3 (Pack Control / Idle Gap) and that the bridge is wired to the H-100's RS-232 PC port (not the RS-485 terminal block, which is a Modbus *master* at factory defaults and won't answer requests).

---

## 4. Install Castle Generator Monitor

On the Pi:

```bash
git clone https://github.com/SethMorrowSoftware/GenWatch.git ~/GenWatch
cd ~/GenWatch
sudo deploy/scripts/install.sh
```

The installer is idempotent — safe to re-run for upgrades. It:

1. Verifies you're root, on Bookworm, on a Pi.
2. Installs apt deps: `python3-venv`, `build-essential`, `nodejs` (>= 18), `npm`, `rsync`.
3. Creates the `genwatch` system user.
4. Builds the React/TypeScript frontend (`vite build` — ~10 s on Pi 5).
5. Creates the Python venv at `/opt/genwatch/venv` and installs backend deps.
6. Copies the backend package to `/opt/genwatch/genwatch/`.
7. Copies the built frontend to `/usr/share/genwatch/ui/`.
8. Provisions `/etc/genwatch/config.yaml` with a random `jwt_secret`.
9. Installs the hardware-watchdog drop-in (`/etc/systemd/system.conf.d/10-genwatch-hwwatchdog.conf`) and re-execs pid 1 so the Pi's BCM2712 watchdog starts being petted. A kernel hang from this point on will hard-reset the Pi within ~15 s.
10. Installs the systemd unit, runs `genwatch doctor` for a pre-flight report, and starts the service (after the admin password is set).

You should see something like:

```
[genwatch] Repository root: /home/pi/GenWatch
[genwatch] Host:            Raspberry Pi 5 Model B Rev 1.0
[genwatch] OS:              debian-bookworm
[genwatch] Installing apt packages: python3-venv python3-dev …
[genwatch] Building frontend bundle …
[genwatch] Installing systemd unit
[genwatch] Running pre-flight diagnostics
== Castle Generator Monitor — doctor (v0.1.0) ==
  Python:    3.11.x
  Config:    /etc/genwatch/config.yaml
  Mock:      False
  Transport: tcp 192.168.1.249:10001
  Auth:      MISSING admin_password_hash — run: genwatch hash <password>
  Registers: /opt/genwatch/genwatch/registers/h100.yaml
             35 read + 5 write, slave=100
  Modbus:    slave 100 responded with [0] (37ms)

⚠  ADMIN PASSWORD NOT SET
```

If you see `Modbus: NO RESPONSE`, jump to [§10 Troubleshooting](#10-troubleshooting). The installer continues regardless — the service just won't start until the admin password is set.

---

## 5. Initial configuration

### 5.1 Set the admin password and the bridge target

```bash
sudo genwatch hash 'pick-a-strong-password'
# → $2b$12$XJZ... (paste this whole line)
sudo nano /etc/genwatch/config.yaml
```

Two things in the editor:

1. Replace `admin_password_hash: "REPLACE_ME"` with the hash you just generated.
2. Confirm the link block points at your bridge:

   ```yaml
   transport: tcp
   modbus_tcp:
     host: 192.168.1.249    # ← your bridge's IP
     port: 10001            # ← bridge's listen port (Lantronix default = 10001)
     framer: rtu
     timeout_s: 1.5         # bump to 3-5s if Wi-Fi adds latency
     connect_timeout_s: 3.0
   ```

   Defaults already match `192.168.1.249:10001`; only edit if yours differs. The Settings page in the UI can also edit this; a service restart is still required.

### 5.2 Start the service

```bash
sudo systemctl restart genwatch
sudo systemctl status genwatch
```

You should see `active (running)`. If not:

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

### 5.4 Verify telemetry is live

The Live view should populate within ~2 seconds with engine state, frequency, voltages, and currents from your H-100. The "Comms" badge in the top-right should be green and showing 100 % success. A red **STALE DATA** badge means the WebSocket dropped or no live update has arrived in ~3 poll intervals — see [§10](#10-troubleshooting).

---

## 6. Verify the Modbus link

The bundled `genwatch doctor` and `genwatch modbusdump` commands let you check the link end-to-end without touching the UI:

```bash
# Full pre-flight: config, register map, DB, and a live Modbus probe
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

# Scan a range and classify each address (helpful when adapting to a
# G-Panel or a dealer-customized firmware)
sudo -u genwatch genwatch scan --start 0x0000 --end 0x07FF
```

If `modbusdump` returns values but they don't match what you see on the H-100 panel, you may have a G-Panel revision (addresses shift) or a dealer-customized firmware. See [§12 Adapting the register map](#12-adapting-the-register-map).

---

## 7. Operation

### Daily use

The Live view is the operator console: engine state, electrical output, control buttons, recent events.

- **Remote Start** — only enabled when state is `stopped`. Two-step confirm with an 8-char hex token that expires in 30 s.
- **Remote Stop** — enabled while running/exercising. Initiates the controller's normal cool-down cycle.
- **Quiet-Test** — 30-minute unloaded exercise. Idle exercise schedule shown at the top right.
- **Transfer back** — while running, hand the load back to utility and cool down.

All commands are Modbus writes:

- **Start / Stop / Transfer** — FC16 multi-register write to `0x019C` (`START_BITS`). Start = `[0x0080, 0x0000, 0x0000]`, stop = `[0x0000, 0x0000, 0x0000]`, transfer = `[0x0080, 0x0000, 0x0080]`.
- **Quiet-Test** — writes `0x0001` to `0x022B` (`QUIETTEST_STATUS`); the same register reads back the test's running status.
- **Acknowledge Alarm** — writes `0x0001` to `0x012E` (`ALARM_ACK`).

Every command is audit-logged with the operator, timestamp, register, the actual word values written, and the result.

### Views

- **Live** — Real-time operator console. Sparklines update every 1.5 s; main telemetry every 15 s. Top-right shows comms health and a STALE DATA badge if the live push has stopped.
- **History** — Chart of any metric over 10 min to 30 days. SQLite-backed, decimated server-side.
- **Events** — Append-only log of state transitions, alarms, comms changes, and operator commands.
- **Settings** — Bridge endpoint, Modbus, register map, retention, Slack alerts. Changes saved to `/etc/genwatch/config.yaml`; the UI warns when a restart is required.

### CLI commands

All exposed via the `genwatch` wrapper installed by the installer:

```bash
genwatch serve                  # run the service (used by systemd)
genwatch hash <password>        # bcrypt-hash a password for config
genwatch gensecret              # generate a JWT signing secret
genwatch doctor                 # pre-flight diagnostics
genwatch modbusdump [--addr]    # read raw registers from the controller
genwatch scan [--start --end]   # walk a range and classify each register
genwatch panel [--json]         # decoded snapshot of every named register —
                                #   side-by-side cross-check vs the H-100 LCD
genwatch version                # print version
```

### Cross-checking against the H-100 LCD

When a value in the UI looks off — a warning that isn't on the panel, a
percentage above 100, a sensor reading you don't trust — `genwatch panel`
reads every register in the loaded map, decodes every bit by its name
(from `engine_state_bits` and `alarm_bits` in `registers/h100.yaml`), and
prints a report you can hold next to the H-100's own display:

```bash
sudo -u genwatch genwatch panel
```

The report shows the derived engine state (with the exact bit that
triggered it), every telemetry value with units and raw hex, every set
bit in each status register labelled with its `code`/severity (or `?`
if the bit isn't in our map for your panel revision), and the list of
currently active alarms. Values flagged with `←` are structurally
suspicious — `0xFFFF` sentinels, percentages above 100, RPM above
redline, etc. — and worth confirming on the panel.

If the panel disagrees with the report on any bit, edit
`/opt/genwatch/genwatch/registers/h100.yaml` to match your panel's
actual bit-to-meaning mapping, then `curl -X POST .../api/registers/reload`
or `sudo systemctl restart genwatch`.

### Useful systemd commands

```bash
sudo systemctl restart genwatch         # restart after config changes
sudo systemctl stop genwatch            # stop
sudo systemctl status genwatch          # status + last 10 log lines
journalctl -u genwatch -e               # follow the log (press q to quit)
journalctl -u genwatch --since "10 min ago"

# Verify the hardware watchdog is petting /dev/watchdog
systemctl show | grep -i watchdog
wdctl                                   # shows SoC watchdog status
```

---

## 8. Security recommendations

Castle Generator Monitor is designed for a **trusted LAN** deployment. By default it listens on `0.0.0.0:8000` over plain HTTP; cookies are not `Secure`. This is appropriate for a Pi sitting in the same building as the generator on a private network. Do not expose port 8000 to the public internet without adding one of the following:

### 8.1 Use Tailscale for remote access

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

Tailscale gives you an encrypted private mesh; the Pi gets an IP like `100.x.y.z` reachable only from your other Tailscale devices. Combined with [Tailscale ACLs](https://tailscale.com/kb/1018/acls) this is more than sufficient for most field deployments. `tailscale cert` will also give you browser-trusted TLS if you want it.

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

Caddy will auto-fetch a Let's Encrypt cert if the hostname resolves publicly. Then change the cookie to `secure=True` in `backend/genwatch/api/auth.py`.

### 8.3 Firewall

```bash
sudo apt-get install -y ufw
sudo ufw allow ssh
sudo ufw allow from 192.168.0.0/16 to any port 8000
sudo ufw enable
```

Restricts the monitor's port to your LAN ranges.

### 8.4 Built-in defenses

- **Login rate-limiter** — 5 attempts then 1 attempt per 3 minutes per source IP. State resets on service restart.
- **JWT secret rotation** — invalidate all sessions by regenerating: `sudo genwatch gensecret` → paste into `config.yaml` `jwt_secret:` → `sudo systemctl restart genwatch`.
- **Audit log** — `/var/lib/genwatch/db.sqlite` table `audit` records every login attempt, confirm-token issue/use, and control command with source IP, operator, and result. SQLite `synchronous=FULL` means a power cut after a command can't lose the audit row.
- **Server-side state validity** — even if the UI bug-allows clicking "Start" while the engine is running, the server rejects with HTTP 409 and audit-logs the denial.
- **Hardened systemd unit** — `NoNewPrivileges`, `ProtectSystem=strict`, `ProtectKernelTunables`, narrow `DeviceAllow` list, `MemoryMax=512M`, `TasksMax=128`.

---

## 9. Updating

The installer is idempotent — re-run any time you pull updates:

```bash
cd ~/GenWatch
git pull
sudo deploy/scripts/install.sh
sudo systemctl restart genwatch
sudo systemctl status genwatch --no-pager
sudo genwatch doctor
```

It will:

- Reinstall apt deps (no-op if current).
- Reinstall the venv deps (only changes if `requirements.txt` moved).
- Rebuild the frontend (only if sources are newer than the dist).
- Sync the backend package.
- Keep your `/etc/genwatch/config.yaml` and `/var/lib/genwatch/db.sqlite` untouched.

If you changed the register map (locally edited `registers/h100.yaml`), hot-reload after login:

```bash
curl -b cookies.txt -X POST http://localhost:8000/api/registers/reload
curl -b cookies.txt http://localhost:8000/api/registers/verify
```

Keep a dated backup of `/etc/genwatch/config.yaml` before major upgrades. The SQLite schema is forward-compatible (`CREATE TABLE IF NOT EXISTS` everywhere) — an upgrade never destroys data.

---

## 10. Troubleshooting

### Symptom: `TCP: CANNOT REACH <host>:<port>` in `genwatch doctor`

The Pi can't even open a TCP connection to the bridge — this is a network or bridge-config problem, not a Modbus problem.

| Check | Command / action |
|-------|------------------|
| **Bridge powered and on the LAN?** | `ping <bridge-ip>` from the Pi. No reply = network or power issue. Check the bridge is plugged into a powered switch port and its status LED is solid. |
| **Bridge listening on the port?** | `nc -vz <bridge-ip> <port>` (typically `10001`). "Connection refused" means the bridge is up but not listening on that port. Log into the bridge web UI → Channel 1 → Connection → confirm **Active Connect = None, Passive Connect = Yes, Local Port = 10001** (or whatever you configured). |
| **Wrong port number?** | Multi-port Lantronix devices use 10001 for port 1, 10002 for port 2, etc. Single-port devices always use 10001. Check the bridge's web UI for the actual Local Port of the channel wired to the H-100. |
| **Firewall between Pi and bridge?** | If they're on different VLANs/subnets, a router ACL may be blocking TCP/10001. `traceroute <bridge-ip>` shows the path. Open the port on the relevant firewall, or move them onto the same subnet. |
| **Other client holding the socket?** | Some bridge configurations only allow one TCP client at a time. If a Windows machine still has CPR holding COM8 → 10001 open, the Pi may get refused. Close the CPR session, or set the bridge to allow multiple connections (Channel 1 → Connection → Endpoint Configuration on Lantronix). |

### Symptom: `Modbus: NO RESPONSE` but the TCP socket connects fine

Bytes are flowing but the H-100 isn't replying — or its reply is being mangled. The TCP layer is fine; this is a serial-side or framing problem at the bridge.

| Check | Command / action |
|-------|------------------|
| **Pack Control splitting RTU frames** *(by far the most common)* | Modbus RTU's end-of-frame is a 3.5-character silence. If the bridge's Pack Control is set aggressively, it'll forward bytes mid-frame and the H-100 sees malformed packets. Lantronix web UI → Channel 1 → Connection → **Pack Control → Idle Gap Time → ~10 ms**. On older Lantronix firmware the equivalent setting may be labeled "Send Characters" or "Force Transmit". |
| Bridge serial settings don't match the H-100 | Bridge web UI → Channel 1 → Serial Settings. Must be **9600 baud, 8 data bits, No parity, 1 stop bit, Flow control: None**. |
| Bridge wired to the wrong panel port | The bridge should be wired to the H-100's RS-232 PC port (sometimes labeled "GenLink", "PC", or "Service"). If it's on the RS-485 terminal block (Mod-485 / A B GND) without a panel reconfiguration via GenLink, the H-100 won't answer — RS-485 is a Modbus *master* at factory defaults. |
| Wrong slave ID | H-100 factory default is 100 (0x64). Check `modbus.slave:` in `/etc/genwatch/config.yaml`. |
| Latency > timeout | LAN-attached bridges add 5–20 ms per request; congested Wi-Fi can blow past 1.5 s. Bump `modbus_tcp.timeout_s` to 3–5 s in `/etc/genwatch/config.yaml` and restart. |

### Symptom: UI shows a red **STALE DATA** badge

The browser is connected but no live update has arrived recently (WebSocket dropped, or the prime poll has gone silent). Hover the badge for the cause; check:

- **WebSocket dropped** — usually a reverse-proxy idle timeout. Check Caddy/nginx settings if you're proxying. The hook auto-reconnects with exponential backoff (max 30 s).
- **Prime poll silent** — the backend got an exception in the poll loop. `journalctl -u genwatch -e | grep -i poll` will show it. The systemd watchdog will SIGKILL and restart within 15 s if it stays silent past ~`6 × prime_poll_ms`.

### Symptom: Comms badge is "LOST" but the service is running

The poller can't get a response from the H-100. Run `sudo genwatch doctor` to isolate whether the bridge is reachable (TCP layer) and whether the H-100 is replying (Modbus layer). The service stays up so you can investigate from the UI — it no longer crashes on a missing link.

### Symptom: Service restart-looping

```bash
journalctl -u genwatch --since "5 minutes ago" --no-pager
```

The systemd unit pets the watchdog from a *prime-poll heartbeat* — if the poll loop hangs (e.g. a pymodbus deadlock on a flaky link), the watchdog stops being pet and systemd SIGKILLs after ~60 s. `RestartSec=30` paces the restart so a permanent fault doesn't burn the SD card. A flapping service usually means a startup-time exception — the log will show it.

### Symptom: SQLite "database is locked"

WAL mode handles concurrent reads fine. Locks only happen if a foreign process (e.g. you opened the DB with `sqlite3` and started a transaction) is holding a write lock. `Ctrl-D` out of that and try again. The service can still read while you peek:

```bash
sudo -u genwatch sqlite3 /var/lib/genwatch/db.sqlite \
  "SELECT * FROM events ORDER BY ts DESC LIMIT 10;"
```

### Symptom: "Connection refused" in the browser

```bash
sudo systemctl status genwatch       # is it running?
sudo ss -tlnp | grep 8000             # is it listening on 8000?
```

If the service is `failed`, `journalctl -u genwatch -e` will show why. Most common: typo in `config.yaml` that fails Pydantic validation at startup.

### Symptom: Under-voltage warnings, kernel messages about power

Pi 5 needs a true 5 V / 5 A supply. Cheap USB-C chargers brown out under USB peripheral load. Use the official 27 W PSU, or measure with a USB power meter (should hold 5.1 V). Under-voltage events can also trip the hardware watchdog reboot — `wdctl` and `dmesg | grep -i watchdog` together tell the story.

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
│   ├─ /api/auth        login, logout, /me                        │
│   ├─ /api/status      full live snapshot                        │
│   ├─ /api/telemetry   time-series (SQLite-backed)               │
│   ├─ /api/events      event/alarm log                           │
│   ├─ /api/control     confirm-token-gated start/stop/etc.       │
│   ├─ /api/config      read/write /etc/genwatch/config.yaml      │
│   └─ /api/registers   read/reload register map                  │
│                                                                  │
│  Two-tier Modbus poller:                                         │
│   • prime (1.5 s): output_status_1..8 bitfields, key switch,    │
│                    quiet-test status, alarm count                │
│   • base  (15 s):  RPM, V, A, Hz, kW, oil P/T, coolant, batt…   │
│  Engine state + active alarms derived from the bitfield bits.    │
│  Coalesces contiguous registers into a single Modbus read.       │
│  Falls back to single-register reads if a batch fails.           │
│                                                                  │
│  State machine + control service:                                │
│   • semantic engine state (stopped/cranking/running/…)           │
│   • two-step confirm tokens (8-char hex, 30 s TTL, single-use)   │
│   • server-side state-validity guards                            │
│   • audit log on every command                                   │
│                                                                  │
│  Storage (SQLite WAL, synchronous=FULL):                         │
│   • telemetry / telemetry_1m / telemetry_1h                      │
│   • events / alarms_active / audit / kv                          │
│   • retention task aggregates and prunes every 5 min             │
└─────────────────────────────┬───────────────────────────────────┘
                              │ Modbus RTU over TCP (raw-TCP tunnel)
                              │ 9600 8N1, slave 100
                              v
                       ┌──────────────┐
                       │ Network      │  Lantronix / Moxa / Digi /
                       │ serial bridge│  ser2net — listening on a TCP
                       └──────┬───────┘  port (typically 10001)
                              │ RS-232 (9600 8N1)
                              v
                       ┌──────────────┐
                       │ H-100        │  Generac H-100 controller
                       │ controller   │  on the generator panel
                       └──────────────┘
```

### Reliability features

- **Hardware watchdog on pid 1** — drop-in at `/etc/systemd/system.conf.d/10-genwatch-hwwatchdog.conf` sets `RuntimeWatchdogSec=15s`. systemd pets the Pi's BCM2712 watchdog via `/dev/watchdog`; a kernel hang, USB controller wedge, or thermal panic hard-resets the Pi within ~15 s.
- **Software watchdog driven by a poll heartbeat** — `Type=notify` unit with `WatchdogSec=60s`. The app only pings `sd_notify(WATCHDOG=1)` while a *prime* Modbus poll has completed within the last ~6 × prime cadence. A deadlocked poll task (pymodbus stuck on a bad socket) lets systemd SIGKILL and restart. Uses a monotonic clock so NTP/DST jumps can't fool the timing.
- **TCP keepalive on the Modbus socket** — `SO_KEEPALIVE` + Linux `TCP_KEEPIDLE=30` / `KEEPINTVL=10` / `KEEPCNT=3`. The kernel drops a wedged socket (Lantronix reboot, NAT idle timeout, switch flap with no FIN/RST) within ~60 s instead of waiting for application read timeouts to exhaust.
- **Graceful degradation when the link is down** — a Modbus connect failure at startup no longer hard-exits. The service stays up with comms shown as `LOST` in the UI; the poller reconnects in the background. Stops systemd restart-thrash from burning the SD card during outages.
- **Batch-read fan-out** — a failing Modbus block read falls back to single-register reads so one bad address can't blank out an entire telemetry tier.
- **SQLite WAL with `synchronous=FULL`** — fsyncs the WAL on every commit, so a power cut on the Pi can't lose freshly committed alarm / audit / event rows.
- **Frontend stale-data indicator** — a red **STALE DATA** badge appears when the WebSocket is down or no live push has arrived in ~3 poll intervals, so operators don't act on frozen numbers.
- **Per-poll timeouts and retries** on every Modbus read; configurable in `config.yaml`.
- **Comms watchdog** — declares LOST after no successful prime poll for 3× the prime cadence.
- **Token replay protection** — confirm tokens are single-use, 30 s TTL, operator-bound, audit-logged on every state transition.
- **Server-side state validity** — every control command re-checks the engine state and rejects with 409 Conflict if invalid (e.g. Start while running).
- **Login rate-limiter** — token-bucket per source IP, 5 burst then 1 per 3 min.
- **Retention** — raw telemetry pruned at 7 d, 1-min rollup at 90 d, info events at 30 d. Alarms / warns and the audit log are never auto-pruned.

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

# Walk a wider range and classify each address (printable ASCII / int /
# bitfield / counter heuristics)
sudo -u genwatch genwatch scan --start 0x0000 --end 0x07FF

# Or probe common H-100 register regions
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

- **static** — map structure / safety issues (overlaps, invalid FC, invalid tier, etc.)
- **live** — per-register Modbus read failures against the currently configured H-100 link

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
git clone https://github.com/SethMorrowSoftware/GenWatch.git
cd GenWatch

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
# → http://127.0.0.1:8000

# Frontend (separate terminal)
cd frontend
npm install
npm run dev      # → http://127.0.0.1:5173 (proxies /api + /ws to :8000)

# Log in with password "dev"
```

The mock client simulates a plausible H-100 — engine state machine, electrical output, alarm injection. Control buttons drive the mock, so the full operator flow works without any hardware.

### Tests

```bash
cd backend
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests/ -v
# Test categories: register decode + batching, e2e mock control flow,
# rate-limit, events retention, sd_notify, poll heartbeat + batch fallback,
# TCP keepalive, refuse-silent-mock safety, Slack notifier
```

### Layout

```
backend/
  genwatch/
    modbus/          register YAML loader, decoder, RTU/TCP client, two-tier poller
    services/        state machine, control, auth, retention, rate-limit, notify
    api/             REST + WebSocket routes
    registers/       h100.yaml — default register map
  tests/             pytest

frontend/
  src/
    api/             typed fetch client
    hooks/           useLiveData (WS + status seed + reconnect + stale flag)
    components/      Icon, Pill, Sparkline, LineChart, Card, Modal, Switch
    views/           Live, History, Events, Settings, Login, ConfirmModal
    styles/          genwatch.css

deploy/
  systemd/genwatch.service                Hardened unit with sd_notify watchdog
  systemd/system.conf.d/                  pid-1 hardware-watchdog drop-in
  udev/99-genwatch-modbus.rules           Stable /dev/genwatch-modbus symlink (legacy)
  scripts/install.sh                      Idempotent installer
  config.yaml.example                     Annotated config template

design_handoff_genwatch/                  Original design spec (reference)
```

### API contract

| Method | Path                                          | Notes                                       |
|--------|-----------------------------------------------|---------------------------------------------|
| GET    | `/api/health`                                 | Liveness; no auth                           |
| POST   | `/api/auth/login`                             | `{ password }` → session cookie             |
| POST   | `/api/auth/logout`                            | Clear cookie                                |
| GET    | `/api/auth/me`                                | Identity (200 even when anonymous)          |
| GET    | `/api/status`                                 | Full live snapshot                          |
| GET    | `/api/telemetry`                              | `?metric=kw&from=&to=&max_points=`          |
| GET    | `/api/events`                                 | `?limit=&severity=alarm,warn`               |
| GET    | `/api/alarms?active=true`                     | Active alarms                               |
| POST   | `/api/alarms/{code}/ack`                      | Operator clears an alarm                    |
| GET    | `/api/alarm-codes`                            | Static reference table                      |
| GET    | `/api/control/confirm`                        | Issue confirm token (op+)                   |
| POST   | `/api/control/{start,stop,exercise,transfer}` | Body `{ confirm_token }`                    |
| GET    | `/api/config`                                 | Effective config (sanitized)                |
| PUT    | `/api/config`                                 | Update on-disk config (admin)               |
| GET    | `/api/registers`                              | Current register map + last read            |
| POST   | `/api/registers/reload`                       | Re-read YAML from disk (admin)              |
| GET    | `/api/registers/verify`                       | Static + live register verification (admin) |
| WS     | `/ws/live`                                    | `snapshot` / `transition` / `alarm`         |

All errors return JSON `{ detail: { code, message } }` with appropriate HTTP status.

---

## 14. License

MIT — see [LICENSE](LICENSE).
