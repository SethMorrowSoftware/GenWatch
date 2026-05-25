# Castle Generator Monitor

Professional monitoring and control software for the **Generac H-100** industrial generator, running on a **Raspberry Pi 5** and talking to the controller over a **Modbus-RTU-over-TCP** network bridge (Lantronix UDS / EDS / xDirect, Moxa NPort, Digi PortServer, ser2net, etc.).

A single-pane operator console: live engine state, electrical output, two-step-confirm controls (start / stop / quiet-test / transfer) gated on the H-100 front-panel key switch, time-series history, alarms, utility-outage tracking, maintenance journal with hour-and-day reminders, fuel-burn estimation, Pi host health, CSV exports, on-device config backup/restore, and on-device configuration of the link, register map, and retention policy.

> **Note on naming.** The product was previously called *GenWatch*. The internal Python package, systemd unit, CLI, and on-disk paths (`/etc/genwatch/`, `genwatch.service`, the `genwatch` CLI) keep those identifiers so existing deployments don't break. Only the operator-facing copy was rebranded.

> **Reliability summary.** Hardware watchdog on pid 1 (Pi reboots on kernel hang); software watchdog on the polling loop driven by a monotonic prime-poll heartbeat (service restarts on a deadlocked read); TCP keepalive on the Modbus socket (dead Lantronix detected in ~60 s); SQLite WAL with `synchronous=FULL` (audit/alarm rows survive a power cut); graceful degradation when the link is down (UI stays reachable, comms shown as LOST, reconnect in the background); panel-mode gate on every remote command (server rejects with 409 unless the H-100 key switch is in AUTO); batch-read fan-out preserves last-good values when a single register fails (no sentinel zeros that could trip an alarm comparator); register-map hot-reload propagates to the live poller without a service restart; login rate-limited; audit log on every control command. Test coverage under `backend/tests/` (97 tests).

> **Feature parity vs genmon.** Modeled after `jgyates/genmon` for register accuracy, but extends with: panel-key-switch gate, two-step confirm tokens, hot-reload, SQLite WAL+FULL durability, single-register fan-out with last-good preservation, hardware + software watchdogs, login rate-limiter, **utility-outage tracker with peak-kW and kWh totals, maintenance journal seeded with 10 default schedule items, fuel-burn estimator (diesel/LP/NG), Pi host-health probes, CSV exports for telemetry / events / audit, and one-click config backup/restore.**

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
- [13. Operational features](#13-operational-features) (outages · fuel · maintenance · host health · CSV · backup)
- [14. Development](#14-development)
- [15. License](#15-license)

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

   Defaults already match `192.168.1.249:10001`; only edit if yours differs. The Settings page in the UI can also edit this; transport / endpoint / retention / Slack changes write straight to `config.yaml` and require a service restart. Register-map edits hot-reload — see [§12](#12-adapting-the-register-map).

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

- **Remote Start** — only enabled when state is `stopped` *and* the H-100 front-panel key switch is in AUTO. Two-step confirm with an 8-char hex token that expires in 30 s.
- **Remote Stop** — enabled while running / exercising / cranking. Initiates the controller's normal cool-down cycle.
- **Quiet-Test** — 30-minute unloaded exercise. Idle exercise schedule shown at the top right.
- **Transfer back** — while running, hand the load back to utility and cool down.

### Panel key-switch gating

The H-100 has a physical key switch on the front panel with three positions: **AUTO / MANUAL / OFF**. The controller only honors remote start/stop/exercise/transfer writes when the switch is in **AUTO**. MANUAL means a local operator at the unit has taken control; OFF means the engine is locked out. Sending a remote command on a panel that isn't in AUTO would succeed at the Modbus wire layer but be silently dropped by the controller — leaving the UI claiming success while nothing happens at the generator.

The monitor handles this on both ends:

- **Topbar chip** (`PANEL · AUTO / MANUAL / OFF / ?`) shows the live key-switch position, decoded from `input_status_1` bits per `panel_mode_bits` in `registers/h100.yaml`. Updates live over the WebSocket, so toggling the switch at the unit refreshes the chip without a page reload.
- **Control buttons** are disabled (with a tooltip hint) whenever the chip is not AUTO.
- **Server-side gate** rejects with `HTTP 409 panel_mode_locked` even if a buggy client bypasses the UI disabled state. Every attempt is audit-logged.

If the chip stays on `?` (unknown) even when the panel is in AUTO, your firmware's bit assignment for the key switch differs from genmon's defaults — see [§12 Adapting the register map](#12-adapting-the-register-map).

### Modbus writes

All commands are Modbus writes against the H-100:

- **Start / Stop / Transfer** — FC16 multi-register write to `0x019C` (`START_BITS`). Start = `[0x0080, 0x0000, 0x0000]`, stop = `[0x0000, 0x0000, 0x0000]`, transfer = `[0x0080, 0x0000, 0x0080]`.
- **Quiet-Test** — writes `0x0001` to `0x022B` (`QUIETTEST_STATUS`); the same register reads back the test's running status.
- **Acknowledge Alarm** — writes `0x0001` to `0x012E` (`ALARM_ACK`).

Every command is audit-logged with the operator, timestamp, action, the actual register + word values written, and the result (`ok` / `denied` / `failed`). Login attempts additionally record the source IP. See [§8.4](#84-built-in-defenses).

### Views

- **Live** — Real-time operator console. Sparklines update every 1.5 s; main telemetry every 15 s. Top-right shows comms health and a STALE DATA badge if the live push has stopped. Includes a Fuel-burn card (diesel/LP/NG load-curve estimate with day/month/lifetime totals + hours-until-empty) and a Host-Health card (CPU temp, memory, disk, throttle flags) so an operator sees the Pi degrading before it stops reporting on the generator.
- **History** — Chart of any metric over 10 min to 30 days. SQLite-backed, decimated server-side. **Export CSV** button downloads the raw rows in the current range with ISO timestamps for Excel / Sheets / Grafana ingestion.
- **Events** — Append-only log of state transitions, alarms, comms changes, and operator commands. **Export CSV** for the current filtered view, plus a one-click **Audit log** download (every login, control command, config edit).
- **Outages** — Auto-detected utility-outage history. Each outage logs start/end timestamp, duration, peak kW, integrated kWh delivered, and a free-form operator notes field. Summary stats over 30 days and 365 days. An open outage shows a "LIVE" chip with the duration counter ticking in real time.
- **Maintenance** — Service journal. Ships with 10 default scheduled items (oil change, oil filter, air filter, fuel filter, coolant, battery test/replace, belt + hose inspection, annual service). Each item tracks both an hours interval and a calendar-day interval — whichever trips first goes overdue. "Mark done" stamps the current run-hours reading. Append-only log shows the service history forever. Admins can add custom items.
- **Settings** — Bridge endpoint, Modbus, register map, retention, Slack alerts, and **Backup · Restore**. Changes saved to `/etc/genwatch/config.yaml`; the UI warns when a restart is required. The Backup tab downloads a tar.gz of `/etc/genwatch` (config + any local register-map edits) for off-site safekeeping, and accepts that same tarball back to seed a fresh Pi.

### CLI commands

All exposed via the `genwatch` wrapper installed by the installer. Run any with no args to see the per-command flags.

```bash
genwatch serve                       # run the service (used by systemd)
genwatch hash <password>             # bcrypt-hash a password for config
genwatch gensecret                   # generate a JWT signing secret (hex)
genwatch doctor [--config PATH]      # pre-flight diagnostics: config, DB, register map,
                                     #   bridge reachability, live Modbus probe
genwatch modbusdump [--addr 0xNN]    # read raw registers from the controller.
        [--count N] [--fc 3|4]       #   --host/--port override config for ad-hoc probes
        [--host IP] [--port N]
genwatch scan [--start 0xNN]         # walk a range and classify each register
        [--end 0xNN] [--fc 3,4]      #   (printable ASCII / integer / bitfield / counter)
        [--batch N] [--out FILE]
genwatch panel [--json] [--html]     # decoded snapshot of every named register vs
                                     #   the H-100 LCD. --html emits a printable
                                     #   cross-check sheet with write-in space.
genwatch version                     # print version
```

All commands except `serve`, `hash`, `gensecret`, and `version` read `/etc/genwatch/config.yaml`. When running by hand, use `sudo -u genwatch …` so the service's config is found and the SQLite path is writable.

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
(see [§12](#12-adapting-the-register-map) for the full hot-reload flow).
The reload propagates to the live poller, state machine, and control
service — no service restart needed for register-map edits.

For a paper-friendly version you can take to the panel, add `--html`:

```bash
sudo -u genwatch genwatch panel --html > /tmp/cross-check.html
# Open /tmp/cross-check.html in any browser → File → Print (or Save as PDF)
```

The HTML sheet is pre-filled with the current live readings and has
write-in space next to each value for you to record what the panel
displays. Sections cover active warnings, high-confidence numeric
cross-checks (battery, run hours, temperatures, fuel %), suspicious
values, unknown bits, and a sign-off block.

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

- **Login rate-limiter** — 5 attempts then 1 attempt per 3 minutes per source IP. State resets on service restart. *(Note: behind a reverse proxy the limiter sees the proxy's IP — restricts the limiter to a single global bucket. Use Tailscale or `ufw` for proxied deploys.)*
- **JWT secret rotation** — invalidate all sessions by regenerating: `sudo genwatch gensecret` → paste into `config.yaml` `jwt_secret:` → `sudo systemctl restart genwatch`. An empty `jwt_secret` makes the service generate an ephemeral one at startup (warning logged); set it explicitly so sessions survive restarts.
- **Audit log** — `/var/lib/genwatch/db.sqlite` table `audit` records every login attempt (with source IP), every confirm-token issue/consume/evict, and every control command (with operator, action, register, word values, and result `ok`/`denied`/`failed`). SQLite `synchronous=FULL` means a power cut after a command can't lose the audit row.
- **Server-side state validity** — every control command re-checks `engine_state` server-side; clicking "Start" while running returns HTTP 409 `invalid_state` and audit-logs the denial.
- **Panel-mode gate** — every remote command re-checks the H-100 front-panel key-switch position; rejects with HTTP 409 `panel_mode_locked` unless the panel is in AUTO. Stops a stolen session (or a misclicked button) from quietly no-op'ing at the unit. See [§7 Panel key-switch gating](#panel-key-switch-gating).
- **Confirm-token discipline** — 8-char hex tokens (`secrets.token_hex(4)`), 30 s TTL, single-use (`pop`-on-consume), operator-bound (issuer must match consumer). Replay returns 400 `token_invalid`.
- **Hardened systemd unit** — `NoNewPrivileges`, `ProtectSystem=strict`, `ProtectKernelTunables`, `RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX`, narrow `DeviceAllow` list (covers FTDI/CH340/CP210x USB-serial chips + the Pi 5 on-board UART for the legacy serial fallback), `MemoryMax=512M`, `TasksMax=128`.

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
- Sync the backend package to `/opt/genwatch/genwatch/`.
- Keep your `/etc/genwatch/config.yaml` and `/var/lib/genwatch/db.sqlite` untouched.

The journal will show `Poller register-map reloaded: prime N→N batches, base N→N batches` if the upgrade includes a YAML edit — the first restart picks it up at boot.

### Register-map edits without a restart

If you've locally edited `registers/h100.yaml` (e.g. fixed a bit position for your firmware revision after running `genwatch panel`), hot-reload while the service keeps running. The reload propagates to the live poller, state machine, and control service:

```bash
# Log in once to get a session cookie
curl -c cookies.txt -X POST http://localhost:8000/api/auth/login \
     -H 'Content-Type: application/json' -d '{"password":"<your-admin-pw>"}'

# Validate the new map (static rule check + per-register live read probe)
curl -b cookies.txt http://localhost:8000/api/registers/verify

# Apply the new map to the running poller — no restart needed
curl -b cookies.txt -X POST http://localhost:8000/api/registers/reload
```

`/api/registers/verify` is read-only and reports both **static** issues (overlaps, invalid FC, missing tier) and **live** failures (per-register Modbus reads against the configured H-100). Use it to commission a YAML edit before flipping the live poller over to it.

### Backups + schema

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

### Symptom: Control buttons greyed out · "Panel key switch is MANUAL"

The H-100 front-panel key switch is not in AUTO. Set the panel to AUTO at the unit; the UI chip refreshes within ~1.5 s over the WebSocket and the buttons re-enable. If the chip stays on `?` (unknown) while the panel is in AUTO, the bit positions in your YAML don't match your firmware — run `sudo -u genwatch genwatch panel` to see the raw `input_status_1` value and edit `panel_mode_bits` in `/opt/genwatch/genwatch/registers/h100.yaml` to match (see [§12](#12-adapting-the-register-map)). The AUTO bit (`0x8000`) is firmly known; MANUAL (`0x4000`) and OFF (`0x2000`) ship as best-guess defaults and may need adjustment.

### Symptom: A telemetry value freezes briefly on a flaky link

If a single Modbus read fails inside a coalesced batch, the poller falls back to single-register reads. Registers whose single-read fallback ALSO fails are *skipped* — the previous value is kept rather than overwritten with `0`. So a coolant temp displayed as 188 °F will simply stay at 188 °F until the next successful read, rather than briefly flicker to 0 °F and trip an alarm comparator. The journal shows `skipping decode of <name> @0x<addr> — fan-out read failed` at debug level. If the freeze persists, run `sudo genwatch doctor` and look at the bridge.

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
│   ├─ /api/registers   read/reload register map                  │
│   ├─ /api/system      Pi host health (CPU temp, disk, memory)   │
│   ├─ /api/fuel        burn rate + day/month/lifetime totals     │
│   ├─ /api/outages     utility outage history + 30/365 summary   │
│   ├─ /api/maintenance schedule / log / due-status               │
│   ├─ /api/*/export    CSV exports (telemetry / events / audit)  │
│   └─ /api/backup/...  config tarball download + restore         │
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
│   • panel-mode tracking (AUTO/MANUAL/OFF) — gates remote writes  │
│   • two-step confirm tokens (8-char hex, 30 s TTL, single-use)   │
│   • server-side state-validity guards (409 invalid_state)        │
│   • server-side panel-mode guard      (409 panel_mode_locked)    │
│   • audit log on every command                                   │
│                                                                  │
│  Storage (SQLite WAL, synchronous=FULL):                         │
│   • telemetry / telemetry_1m / telemetry_1h                      │
│   • events / alarms_active / audit / kv                          │
│   • outages (peak_kw, kwh, duration_s per utility outage)        │
│   • maintenance_schedule / maintenance_log (service journal)     │
│   • retention task aggregates and prunes every 5 min             │
│                                                                  │
│  Background services:                                            │
│   • OutageTracker — opens/closes rows on engine-state transitions│
│   • FuelAccumulator — integrates kW × load-curve into gal / scf │
│   • MaintenanceMonitor — hourly due-state scan, emits MAINT     │
│                          events on OK→soon→overdue transitions   │
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
- **Batch-read fan-out, no sentinel zeros** — a failing block read falls back to single-register reads so one bad address can't blank out an entire telemetry tier. Registers whose fan-out *also* fails are skipped (the previous value is kept) rather than overwritten with `0` — a 0 °F on a coolant-temp register could otherwise trip an out-of-range alarm comparator on a transient bus error.
- **Register-map hot-reload** — `POST /api/registers/reload` re-derives the prime/base batch tables under a lock and swaps them into the live poller, state machine, and control service. Operators can fix a bit position or scale and apply it without dropping a poll. Verified by `POST /api/registers/verify` (static + live read probe).
- **SQLite WAL with `synchronous=FULL`** — fsyncs the WAL on every commit, so a power cut on the Pi can't lose freshly committed alarm / audit / event rows.
- **Frontend stale-data indicator** — a red **STALE DATA** badge appears when the WebSocket is down or no live push has arrived in ~3 poll intervals, so operators don't act on frozen numbers. WebSocket reconnects with exponential backoff (cap 30 s).
- **Per-poll timeouts and retries** on every Modbus read; configurable in `config.yaml` (`modbus_tcp.timeout_s`, `modbus.retries`, `modbus.backoff_s`).
- **Comms watchdog** — declares LOST after no successful prime poll for 3× the prime cadence; emits a `comms` event over the WebSocket so the badge transitions live.
- **Two-step confirm tokens** — 8-char hex, 30 s TTL, single-use (`pop`-on-consume), operator-bound. Every issue / consume / expiry / mismatch is audit-logged.
- **Server-side state validity + panel-mode gate** — every remote command re-checks `engine_state` (rejects 409 `invalid_state` for impossible transitions) and the H-100 panel key-switch position (rejects 409 `panel_mode_locked` unless AUTO).
- **Login rate-limiter** — token-bucket per source IP, 5 burst then 1 per 3 min. Returns `429` with `Retry-After`.
- **Retention** — raw telemetry pruned at 7 d, 1-min rollup at 90 d, info / ok events at 30 d. Alarms, warns, and the audit log are never auto-pruned.

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

Then verify the new map, then hot-reload (admin auth required):

```bash
# Log in once to get a session cookie
curl -c cookies.txt -X POST http://localhost:8000/api/auth/login \
     -H 'Content-Type: application/json' -d '{"password":"<admin-pw>"}'

# Static + live verification — read-only, doesn't affect the poller
curl -b cookies.txt http://localhost:8000/api/registers/verify

# Apply to the running poller (re-derives batch tables under a lock,
# swaps into state machine + control service atomically)
curl -b cookies.txt -X POST http://localhost:8000/api/registers/reload
```

`/api/registers/verify` reports:

- **static** — map structure / safety issues (overlaps, invalid FC, invalid tier, control-on-read-address warnings, etc.)
- **live** — per-register Modbus read failures against the currently configured H-100 link (skipped in mock mode)

This makes commissioning safer: edit YAML → verify → reload. The reload propagates to the live poller's prime/base batch tables, the state machine's rule references, and the control service's address resolution — no service restart needed.

The YAML schema is documented in comments at the top of `h100.yaml`. Key sections:

- **`registers`** — per register: `addr`, `fc` (3/4), `type` (`u16`/`s16`/`u32`/`s32`/`bitfld`/`enum`), `scale`, `tier` (`prime`/`base`), `group`, `unit`, `warn_range`, `alarm_range`. Most H-100 telemetry slots are 2-register `u32` blocks; the meaningful value lives in the low word and the decoder reads them as big-endian.
- **`engine_state_bits`** — priority-ordered rules mapping bitfield bits to engine states (`stopped` / `cranking` / `running` / `cooling` / `exercising` / `alarm`). First matching rule wins. List `alarm` rules ahead of `running` so a faulted-while-running engine reports `alarm`, not `running`.
- **`alarm_bits`** — flat table of alarm bits across `output_status_1..8`. Each entry has `register`, `mask`, `code`, `desc`, `severity` (`alarm`/`warn`). Multiple alarms can be active simultaneously; the state machine tracks them as a set and emits `alarm` / `alarm-cleared` events.
- **`panel_mode_bits`** — rules mapping `input_status_1` bits to the H-100 front-panel key-switch position (`auto` / `manual` / `off`). First match wins; non-match → `unknown`. **AUTO (`0x8000`) is firmly known. MANUAL (`0x4000`) and OFF (`0x2000`) ship as best-guess defaults — verify on your unit during commissioning** by toggling the physical switch while watching `genwatch panel` or the topbar chip. The control service rejects every remote write unless this resolves to `auto`, so getting these bits right is required before remote control is usable.
- **`controls`** — write-gated commands. Single-register writes use `value: N` with `fc: 6`; multi-register writes (H-100 start/stop/transfer at `0x019C`) use `values: [w1, w2, w3]` with `fc: 16`. The validator emits a warning when a control's address overlaps a read register (H-100's `0x022B` quiet-test status / control and `0x012E` alarm-ack are intentional duals).

---

## 13. Operational features

The features below extend a plain Modbus monitor into a full operations
journal. They run automatically — no extra config needed beyond the
defaults shipped in `/etc/genwatch/config.yaml` and the
`fuel_type` field on the register YAML's `site:` block.

### 13.1 Utility-outage tracker

Auto-detects when the H-100 starts in response to a utility outage:
the engine transitions `stopped → cranking → running` while the
front-panel key switch is in AUTO. (MANUAL starts are operator-driven
and not counted as outages.) An open outage row accumulates peak kW
and integrated kWh for as long as the engine is producing power. The
next transition out of `running` closes the row with a duration stamp.

The **Outages** view shows two summary cards (30 days, 365 days) and a
table of every recorded outage with a free-form notes field per row.
An in-progress outage shows a `LIVE` chip and ticks its duration in
real time. Backend state survives a service restart — an open row is
re-attached on boot rather than orphaned.

```bash
# REST: list outages
curl -b cookies.txt http://localhost:8000/api/outages | jq

# REST: annotate an outage
curl -b cookies.txt -X POST http://localhost:8000/api/outages/42/notes \
     -H 'Content-Type: application/json' \
     -d '{"notes": "ice storm, neighbourhood down ~4h"}'
```

### 13.2 Maintenance journal

`backend/genwatch/services/maintenance.py` ships with ten default
scheduled items derived from the Cummins QSB7-G5 service manual: oil
change (250 h / annually), oil filter (250 h), air filter (500 h),
fuel filter (500 h), coolant flush (2000 h / 5 y), battery replace
(3 y), battery test (180 d), belt + hose inspection (1000 h /
annually), full annual service. Each item carries both an hours
trigger and a calendar-day trigger — whichever trips first goes
overdue.

The **Maintenance** view shows each item's status (`ok` /
`soon` / `overdue` / `never`) computed from the most recent log entry's
`run_hours_at` against the current run-hours reading and against
`now() - last_ts` for the day trigger. The "Mark done" button records a
new log entry stamped with the current run-hours reading; entries are
append-only so the service history is preserved forever.

Admins can add custom items (e.g. `dpf_clean`, `ats_inspect`) via the
schedule editor on the same page, or directly via REST:

```bash
curl -b cookies.txt -X PUT http://localhost:8000/api/maintenance/schedule \
     -H 'Content-Type: application/json' \
     -d '{"kind": "ats_inspect", "interval_hours": 0, "interval_days": 365, "notes": "Inspect HTS-1 contactor"}'
```

A background `MaintenanceMonitor` re-scans hourly and emits `MAINT`
events into the standard event log on every `ok → soon → overdue`
transition — so the Slack channel and Events feed pick them up
automatically.

### 13.3 Fuel-burn estimator

A simple load-curve model (`services/fuel.py`) converts the live kW
reading into a gallons-per-hour (diesel/LP) or scf-per-hour (NG)
estimate. The accumulator integrates that rate over time into three
running totals: since local midnight (day), since the first of the
month (month), and lifetime (since the service started). At the
current rate, when a tank percentage is available, it also projects
how many hours remain until empty.

Set the fuel type in `registers/h100.yaml`'s `site:` block:

```yaml
site:
  # ...
  fuel_type: diesel   # diesel | lp | ng
```

Defaults:
- **diesel**: 0.6 gph idle + 0.07 gph/kW (~14 gph at 200 kW)
- **lp**:    0.85 gph idle + 0.10 gph/kW
- **ng**:    30 scfh idle + 10 scfh/kW

Accuracy: ±10–15 % on a steady load. Useful for *"are we burning more
fuel this month than last"* trending; not a custody-transfer meter.
The model intentionally doesn't chase exact spec curves (which vary
with altitude, temperature, and load history).

Surfaced on the Live view's *Fuel burn* card and at `GET /api/fuel`.

### 13.4 Host (Pi) health

`GET /api/system` returns CPU temperature, load averages, memory
utilisation, disk utilisation on the SQLite data dir, system + service
uptime, service RSS, and the Pi's firmware-reported throttle bits
(`under_voltage`, `arm_freq_capped`, `throttled`, `soft_temp_limit` —
both "right now" and "since last reboot" flavours).

Surfaced on the Live view's *Host health* card with severity colouring
(amber ≥ 70 °C / 85 % util, red ≥ 80 °C / 95 % util). Lets an operator
see the Pi degrading — under-voltage on a cheap PSU, SD card filling
up, runaway log — *before* the monitor stops reporting.

Probes are pure `/proc` / `/sys` reads with no shell-out, so the
endpoint is safe to poll every ~10 s.

### 13.5 CSV exports

Three streaming CSV endpoints with UTF-8 BOM (so Excel double-clicks
correctly), ISO-8601 timestamps in local TZ plus epoch-seconds, and a
hard cap of 500 k rows per pull:

- `GET /api/telemetry/export?from=<epoch>&to=<epoch>&columns=kw,rpm`
- `GET /api/events/export?from=<epoch>&to=<epoch>&severity=alarm,warn`
- `GET /api/audit/export?from=<epoch>&to=<epoch>`

UI: an **Export CSV** button on the History view (uses the chart's
current range and metric) and on the Events view (uses the active
filters), plus a separate **Audit log** download for compliance use.

### 13.6 Config backup + restore

`Settings → Backup · Restore` downloads a gzipped tar of
`/etc/genwatch` (config.yaml + any local register-map edits + a
MANIFEST.yaml with the source hostname and version). The same tarball
can be uploaded back on a fresh Pi after a hardware swap; the restore
endpoint refuses path-traversal members and atomically writes each
file. The SQLite database is **not** included — that file is big and
has its own rsync-nightly story.

```bash
# CLI alternative (cookie auth required)
curl -b cookies.txt -o backup.tar.gz http://localhost:8000/api/backup/download

# Inspect contents
tar tzf backup.tar.gz
# genwatch/config.yaml
# genwatch/MANIFEST.yaml
# genwatch/registers/h100.yaml   (if locally edited)

# Restore
curl -b cookies.txt -F file=@backup.tar.gz \
     http://localhost:8000/api/backup/restore
sudo systemctl restart genwatch
```

---

## 14. Development

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
```

71 tests across four files:

- `test_registers.py` — YAML loader, decoder for every `RegType`, batch coalescing, address-overlap + bad-FC validation.
- `test_endtoend.py` — boots the app with the mock client, drives the full operator flow (login → confirm → start → state-validity rejection → panel-mode-locked rejection), and verifies `/api/registers/reload` propagates to the live poller / state machine / control service.
- `test_hardening.py` — rate-limiter math, events retention, `sd_notify` parsing, transport selection, TCP keepalive socket options, poller heartbeat stamping, batch-fallback behavior, fan-out-failure preserves last-good value, `panel` CLI command output (text + JSON).
- `test_slack.py` — block builder, gating flags, dispatch worker, retry-on-transport-error vs no-retry-on-Slack-error, token sanitization (never echoed to audit), hot-reload from `PUT /api/config`.

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

The auth column reflects the *current* implementation. Read endpoints are **public** under the trusted-LAN deployment model — anyone with network access to port 8000 can read telemetry, events, and the sanitized config without logging in. Write endpoints (control, config edits, alarm-ack, register reload) require a session cookie. Deploying outside a trusted LAN means putting the monitor behind Tailscale, Caddy, or a firewall ACL per [§8](#8-security-recommendations).

| Method | Path                                          | Auth   | Notes                                                       |
|--------|-----------------------------------------------|--------|-------------------------------------------------------------|
| GET    | `/api/health`                                 | public | Liveness; returns comms state, uptime, mock flag, version   |
| POST   | `/api/auth/login`                             | public | `{ password }` → session cookie (rate-limited per IP)       |
| POST   | `/api/auth/logout`                            | public | Clear cookie                                                |
| GET    | `/api/auth/me`                                | public | Identity (200 with `{authenticated: false}` when anonymous) |
| GET    | `/api/status`                                 | public | Full live snapshot (engine, comms, reading, panel, alarms)  |
| GET    | `/api/telemetry`                              | public | `?metric=&from=&to=&max_points=` (server-side decimation)   |
| GET    | `/api/telemetry/columns`                      | public | Available telemetry metric names                            |
| GET    | `/api/columns`                                | public | Register-name → DB column mapping                           |
| GET    | `/api/events`                                 | public | `?limit=&severity=alarm,warn&type=&from=&to=`               |
| GET    | `/api/alarms?active=true`                     | public | Currently-active alarms                                     |
| POST   | `/api/alarms/{code}/ack`                      | op+    | Operator clears an alarm (writes `0x0001` → `0x012E`)       |
| GET    | `/api/alarm-codes`                            | public | Static alarm-code reference table from the YAML             |
| GET    | `/api/control/confirm`                        | op+    | Issue 8-char hex confirm token (30 s TTL, single-use)       |
| POST   | `/api/control/{start,stop,exercise,transfer}` | op+    | Body `{ confirm_token }`; 409 on invalid state or panel ≠ AUTO |
| GET    | `/api/config`                                 | public | Effective config (bot_token + jwt_secret never returned)    |
| PUT    | `/api/config`                                 | admin  | Update on-disk config; Slack hot-reloads, others need restart |
| POST   | `/api/slack/test`                             | admin  | Send a synchronous test message; returns `{ok, detail}`     |
| GET    | `/api/registers`                              | public | Current register map + last-read values for each            |
| POST   | `/api/registers/reload`                       | admin  | Re-parse YAML, propagate to live poller + state + control   |
| GET    | `/api/registers/verify`                       | admin  | Static + live read verification (skipped in mock mode)      |
| WS     | `/ws/live`                                    | cookie | Pushes `hello` / `snapshot` / `transition` / `alarm` / `alarm-cleared` / `comms` / `ping` |

Roles: **viewer** (read), **operator** (read + control), **admin** (everything including config edits). The default `operator_name` (`auth.operator_name` in config) is the only configured account; the role attached at login is `admin` for the operator account by default — viewer/operator are reserved for future multi-user expansion.

All errors return JSON `{ detail: { code, message } }` with appropriate HTTP status. Common error codes:

| Status | Code                  | Cause                                                            |
|--------|-----------------------|------------------------------------------------------------------|
| 400    | `token_invalid`       | Confirm token missing, expired, or already consumed              |
| 400    | `token_expired`       | Confirm token's 30 s TTL elapsed                                 |
| 401    | `unauthorized`        | No / invalid session cookie                                      |
| 403    | `forbidden`           | Role insufficient for the action                                 |
| 403    | `token_mismatch`      | Confirm token was issued to a different operator                 |
| 409    | `invalid_state`       | Control verb not valid for current engine state (e.g. start while running) |
| 409    | `panel_mode_locked`   | Panel key switch is MANUAL / OFF / unknown — remote writes blocked |
| 429    | `rate_limited`        | Too many login attempts; `Retry-After` header gives the wait    |
| 502    | `modbus_failed`       | Underlying Modbus write returned an error                        |

---

## 15. License

MIT — see [LICENSE](LICENSE).
