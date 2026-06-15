# Castle Generator Monitor

Professional monitoring and control software for the **Generac H-100** industrial generator, running on a **Raspberry Pi 5** and talking to the controller over a **Modbus-RTU-over-TCP** network bridge (Lantronix UDS / EDS / xDirect, Moxa NPort, Digi PortServer, ser2net, etc.).

A single-pane operator console: live engine state, electrical output, two-step-confirm controls (start / stop / quiet-test / transfer) gated on the H-100 front-panel key switch, time-series history, alarms, and on-device configuration of the link, register map, and retention policy.

> **Note on naming.** The product was previously called *GenWatch*. The internal Python package, systemd unit, CLI, and on-disk paths (`/etc/genwatch/`, `genwatch.service`, the `genwatch` CLI) keep those identifiers so existing deployments don't break. Only the operator-facing copy was rebranded.

> **Reliability summary.** Hardware watchdog on pid 1 (Pi reboots on kernel hang); software watchdog on the polling loop driven by a monotonic prime-poll heartbeat (service restarts on a deadlocked read); TCP keepalive on the Modbus socket (dead Lantronix detected in ~60 s); SQLite WAL with `synchronous=FULL` (audit/alarm rows survive a power cut); graceful degradation when the link is down (UI stays reachable, comms shown as LOST, reconnect in the background); panel-mode gate on every remote command (server rejects with 409 unless the H-100 key switch is in AUTO); freshness gate so remote start/stop/ack are rejected when the H-100 link is LOST rather than firing against a last-known engine state; placeholder/weak `jwt_secret` and missing/`REPLACE_ME` `admin_password_hash` refuse to boot in production; batch-read fan-out preserves last-good values when a single register fails (no sentinel zeros that could trip an alarm comparator), with per-register TTL so a stale value can't masquerade as fresh forever; register-map hot-reload propagates to the live poller without a service restart; confirm-token gate on every state-changing endpoint including alarm-ack; CSRF middleware on every mutating `/api/*` request; SQLite uses a single persistent WAL writer connection (telemetry + retention writes off the event loop, lock-free concurrent reads, periodic `wal_checkpoint(TRUNCATE)` to bound the WAL) so a checkpoint can't stall the next poll; Modbus client lock released between retry attempts so an operator command can pre-empt a degraded-link retry chain; short/truncated Modbus frames count as failures (never zero-extend a decode), the watchdog heartbeat is gated on the engine-state registers decoding fresh, and 16-bit-in-u32 sensors decode low-word-only so a framing slip can't inflate a reading; confirm tokens are 128-bit with a monotonic-clock TTL; `GENWATCH_*` env vars correctly override `config.yaml`; Slack notifier dedupes flapping alarms within a 60 s window, drops the oldest (not newest) on overflow, and abandons stale retries past 5 min; supply chain pinned with `package-lock.json` + hash-locked `requirements.lock` (install refuses to run unpinned) and `npm ci --ignore-scripts` on every install; confirm tokens are verb-bound (a token issued for one action can't be spent on another); login rate-limited; audit log on every control command. Test coverage under `backend/tests/` (232 tests).

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
sudo genwatch hash                            # interactive prompt → bcrypt hash
sudo nano /etc/genwatch/config.yaml           # paste the hash, set modbus_tcp.host

# 4. Start it and verify
sudo systemctl restart genwatch
sudo genwatch doctor                          # expect "Modbus: slave 100 responded"
```

Then open `http://<your-pi-ip>:8000` and log in. The Live view should populate within ~2 s.

> **Bringing this live against real hardware?** Follow the step-by-step
> field procedure in **[`docs/COMMISSIONING.md`](docs/COMMISSIONING.md)** —
> it walks the read-only verification first, then controls, then (if
> fitted) the ATS-Pi/ADAM-6060, with safety gates, pass/fail criteria,
> and a sign-off sheet. The quick start above is enough for a bench/mock
> run; the runbook is what makes a plant deployment safe.

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

1. Verifies you're root, on Bookworm or Trixie, on a Pi. Unsupported distros now error out (override with `GENWATCH_ALLOW_UNSUPPORTED_OS=1`); pre-Bookworm systemd silently rejects directives in the hwwatchdog drop-in, which previously failed quietly.
2. Installs apt deps: `python3-venv`, `build-essential`, `nodejs` (>= 18), `npm`, `rsync`.
3. Creates the `genwatch` system user.
4. Builds the React/TypeScript frontend with `npm ci --ignore-scripts` (refuses to drift from the committed `package-lock.json`; blocks postinstall scripts from running under root) followed by `vite build` — ~10 s on Pi 5.
5. Creates the Python venv at `/opt/genwatch/venv` and installs backend deps from `backend/requirements.lock` with `pip install --require-hashes` (every wheel verified against the committed sha256). Falls back to `requirements.txt` with a warning for older clones that predate the lockfile.
6. Copies the backend package to `/opt/genwatch/genwatch/`.
7. Copies the built frontend to `/usr/share/genwatch/ui/`.
8. Provisions `/etc/genwatch/config.yaml` with a random `jwt_secret` (256 bits from CPython's CSPRNG) and `0640 genwatch:genwatch` perms.
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
sudo genwatch hash                            # prompts for the password (no echo)
# → $2b$12$XJZ... (paste this whole line)
sudo nano /etc/genwatch/config.yaml
```

Running `genwatch hash <password>` with the password as an argv arg still works for scripted provisioning, but it leaks the plaintext into `~/.bash_history` and is briefly visible in `ps aux` — the interactive form above is the recommended path.

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
- **Acknowledge Alarm** — writes `0x0001` to `0x012E` (`ALARM_ACK`). Requires a fresh confirm token (same gate as start/stop) so a misclick on an active shutdown alarm can't silently re-enable a remote-start path. The frontend chains `confirm → ack` transparently for the operator.

Every command is audit-logged with the operator, timestamp, action, the actual register + word values written, and the result (`ok` / `denied` / `failed`). Login attempts additionally record the source IP. See [§8.4](#84-built-in-defenses).

### Views

- **Live** — Real-time operator console. Sparklines update every 1.5 s; main telemetry every 15 s. Top-right shows comms health and a STALE DATA badge if the live push has stopped.
- **History** — Chart of any metric over 10 min to 30 days. SQLite-backed, decimated server-side.
- **Events** — Append-only log of state transitions, alarms, comms changes, and operator commands.
- **Settings** — Bridge endpoint, Modbus, register map, retention, Slack alerts. Changes saved to `/etc/genwatch/config.yaml`; the UI warns when a restart is required.

### CLI commands

All exposed via the `genwatch` wrapper installed by the installer. Run any with no args to see the per-command flags.

```bash
genwatch serve                       # run the service (used by systemd)
genwatch hash [<password>]           # bcrypt-hash a password for config (prompts if omitted)
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

Castle Generator Monitor is designed for a **trusted LAN** deployment. By default it listens on `0.0.0.0:8000` over plain HTTP and issues the session cookie with `HttpOnly` + `SameSite=Strict`; the `Secure` flag is added automatically when the request reached us over HTTPS (Caddy or `tailscale serve` in front of the monitor). This is appropriate for a Pi sitting in the same building as the generator on a private network. Do not expose port 8000 to the public internet without adding one of the following:

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

Caddy will auto-fetch a Let's Encrypt cert if the hostname resolves publicly. Once TLS is in front, the session cookie is automatically issued with the `Secure` attribute — no edit to the service required (uvicorn honors `X-Forwarded-Proto` from localhost upstreams and the cookie logic resolves `Secure` from the request scheme). If your reverse proxy doesn't forward `X-Forwarded-Proto`, set `auth.cookie_secure: true` in `/etc/genwatch/config.yaml`.

### 8.3 Firewall

```bash
sudo apt-get install -y ufw
sudo ufw allow ssh
sudo ufw allow from 192.168.0.0/16 to any port 8000
sudo ufw enable
```

Restricts the monitor's port to your LAN ranges.

### 8.4 Built-in defenses

- **Authentication required on every `/api/*` endpoint** except `/api/health` (and the auth endpoints themselves). The trusted-LAN deployment model no longer leaves telemetry, events, alarms, and config readable to anonymous LAN clients. `/api/health` stays open for external uptime monitoring but only returns `{ok, mock}` to anon callers — version, comms state, and DB size require a session.
- **CSRF middleware** rejects any POST/PUT/DELETE/PATCH to `/api/*` whose `Origin` (or `Referer`) header isn't the request's own host or an entry in `cors_origins`. Defense in depth on top of `SameSite=Strict` so a misconfigured `cookie_samesite: lax` doesn't open a CSRF hole. Non-browser clients (curl, ansible) without `Origin`/`Referer` pass through — CSRF requires a victim browser.
- **Login rate-limiter** — 5 attempts then 1 attempt per 3 minutes per source IP. State resets on service restart. *(Note: behind a reverse proxy the limiter sees the proxy's IP — restricts the limiter to a single global bucket. Use Tailscale or `ufw` for proxied deploys.)*
- **JWT secret refuses-to-boot when empty in production.** `sudo genwatch gensecret` → paste into `config.yaml` `jwt_secret:` → `sudo systemctl restart genwatch` rotates all sessions. Previously an empty secret silently generated an ephemeral one and logged a warning; that hid genuine config corruption under `Restart=always`. Mock mode (`GENWATCH_MOCK=true`) preserves the ephemeral fallback for dev / CI.
- **Audit log** — `/var/lib/genwatch/db.sqlite` table `audit` records every login attempt (with source IP), every confirm-token issue/consume/evict, and every control command (with operator, action, register, word values, and result `ok`/`denied`/`failed`). SQLite `synchronous=FULL` means a power cut after a command can't lose the audit row.
- **Server-side state validity** — every control command re-checks `engine_state` server-side *inside* the control lock (so two concurrent requests can't both pass the gate against a stale snapshot). Clicking "Start" while running returns HTTP 409 `invalid_state` and audit-logs the denial.
- **Panel-mode gate** — every remote command re-checks the H-100 front-panel key-switch position; rejects with HTTP 409 `panel_mode_locked` unless the panel is in AUTO. Stops a stolen session (or a misclicked button) from quietly no-op'ing at the unit. See [§7 Panel key-switch gating](#panel-key-switch-gating).
- **Confirm-token discipline** — 8-char hex tokens (`secrets.token_hex(4)`), 30 s TTL, single-use (`pop`-on-consume), operator-bound (issuer must match consumer), required on start / stop / exercise / transfer AND on alarm-ack. Replay returns 400 `token_invalid`.
- **WebSocket hardening** — `/ws/live` validates `Origin` against the same allowlist as the CSRF middleware before accepting (browser-initiated cross-origin connects rejected), re-validates the session JWT every 60 s inside the message loop (an expired or secret-rotated token can't keep streaming live data through a long-lived socket), and logs a deprecation warning when the legacy `?token=` query parameter is used.
- **Session cookie hardening** — `HttpOnly` (no JS access), `SameSite=Strict` by default, `Path=/`. The `Secure` flag is added automatically when the request reached the service over HTTPS (a Caddy / `tailscale serve` deployment terminating TLS gets it for free); override via `auth.cookie_secure: true|false` and `auth.cookie_samesite: strict|lax|none` if your topology needs it. SameSite=None is rejected at config load unless paired with Secure.
- **Hardened systemd unit** — `NoNewPrivileges`, `ProtectSystem=strict`, `ProtectKernel*`, `ProtectClock`, `ProtectHostname`, `ProtectProc=invisible`, `ProcSubset=pid`, `RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX`, empty `CapabilityBoundingSet` / `AmbientCapabilities`, `UMask=0027`, `SystemCallFilter=@system-service` with `~@mount @swap @reboot @raw-io @cpu-emulation @obsolete` negation, `OOMScoreAdjust=-200`, narrow `DeviceAllow` list (covers FTDI/CH340/CP210x USB-serial chips + the Pi 5 on-board UART for the legacy serial fallback), `MemoryMax=512M`, `TasksMax=128`.
- **Supply-chain pinning** — `package-lock.json` and `backend/requirements.lock` (with sha256 hashes for every transitive) are both committed. `install.sh` uses `npm ci --ignore-scripts` (refuses to drift, blocks postinstall scripts from running under root) and `pip install --require-hashes` (refuses a wheel whose sha256 doesn't match a typosquat / compromised mirror).

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
- Reinstall the venv deps from `backend/requirements.lock` with `--require-hashes` (only changes if the lock moved; refuses to install if a transitive sha256 fails to verify).
- Rebuild the frontend with `npm ci --ignore-scripts` (only if sources are newer than the dist).
- Sync the backend package to `/opt/genwatch/genwatch/`.
- Keep your `/etc/genwatch/config.yaml` and `/var/lib/genwatch/db.sqlite` untouched.

The journal will show `Poller register-map reloaded: prime N→N batches, base N→N batches` if the upgrade includes a YAML edit — the first restart picks it up at boot.

### Bumping a Python dep (lockfile regen)

`requirements.lock` is authoritative on install. If you bump a version in `backend/requirements.txt`, regenerate the lock alongside it:

```bash
pip install pip-tools                                          # one-time
pip-compile --generate-hashes \
    --output-file=backend/requirements.lock backend/requirements.txt
```

Commit both files together. Skipping this step means `install.sh` either keeps installing the old version (lockfile wins) or errors out on a hash mismatch — both safe failures, neither what you wanted.

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

Keep a dated backup of `/etc/genwatch/config.yaml` before major upgrades — it contains the `jwt_secret`, the `admin_password_hash`, and (if configured) the Slack `bot_token`.

The SQLite database at `/var/lib/genwatch/db.sqlite` carries the audit log + history. Lose it and you lose forensic records of every operator command — back it up before SD-card rotations:

```bash
# Online backup — SQLite WAL handles this safely while the service runs
sudo -u genwatch sqlite3 /var/lib/genwatch/db.sqlite \
    ".backup /var/lib/genwatch/db.backup-$(date +%F).sqlite"

# Restore (service must be stopped)
sudo systemctl stop genwatch
sudo cp /path/to/db.backup-YYYY-MM-DD.sqlite /var/lib/genwatch/db.sqlite
sudo chown genwatch:genwatch /var/lib/genwatch/db.sqlite
sudo systemctl start genwatch
```

The SQLite schema is forward-compatible (`CREATE TABLE IF NOT EXISTS` everywhere) — an upgrade never destroys data.

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

### Symptom: Service exits with `auth.jwt_secret is unset … in production mode`

The config has `jwt_secret: ""`, the literal `REPLACE_ME` placeholder, or a value shorter than 32 chars (the key is also rejected if missing). Production refuses to start rather than sign sessions with a guessable key — a placeholder secret is a world-known signing key that lets anyone forge an operator session. Generate a real one:

```bash
sudo genwatch gensecret                      # prints hex on stdout
sudo nano /etc/genwatch/config.yaml          # paste into auth.jwt_secret
sudo systemctl restart genwatch
```

For dev / CI, set `GENWATCH_MOCK=true` to re-enable the ephemeral fallback.

### Symptom: Service exits with `auth.admin_password_hash is unset or still the 'REPLACE_ME' placeholder`

No usable admin password is set. Production refuses to start with a missing / placeholder / non-bcrypt hash, because the service would otherwise come up "healthy" while every login returns 401 — a silent lockout. Set it:

```bash
sudo genwatch hash                           # prompts (no echo) → $2b$... hash
sudo nano /etc/genwatch/config.yaml          # paste into auth.admin_password_hash
sudo systemctl restart genwatch
```

### Symptom: Install fails with `Unsupported OS tag`

`install.sh` errors on anything older than Bookworm because pre-243 systemd silently rejects `RebootWatchdogSec` in the hwwatchdog drop-in, leaving shutdown unprotected. If you have a good reason to run on an older distro: `sudo GENWATCH_ALLOW_UNSUPPORTED_OS=1 deploy/scripts/install.sh` — accept that the hwwatchdog may not configure correctly.

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
│   ├─ CSRF middleware on every mutating /api/* request           │
│   ├─ /api/auth        login, logout, /me                        │
│   ├─ /api/status      full live snapshot   (auth required)      │
│   ├─ /api/telemetry   time-series          (auth required)      │
│   ├─ /api/events      event/alarm log      (auth required)      │
│   ├─ /api/control     confirm-token-gated start/stop/etc.       │
│   ├─ /api/alarms/*/ack confirm-token-gated alarm acknowledge    │
│   ├─ /api/config      read/write /etc/genwatch/config.yaml      │
│   ├─ /api/registers   read/reload register map                  │
│   └─ /api/health      anonymous liveness probe (trimmed payload)│
│                                                                  │
│  Two-tier Modbus poller (with per-register TTL on last-good):   │
│   • prime (1.5 s): output_status_1..8 bitfields, key switch,    │
│                    quiet-test status, alarm count                │
│   • base  (15 s):  RPM, V, A, Hz, kW, oil P/T, coolant, batt…   │
│  Engine state + active alarms derived from the bitfield bits.    │
│  Coalesces contiguous registers into a single Modbus read.       │
│  Falls back to single-register reads if a batch fails. Values    │
│  evicted from `reading.values` past 3× their tier cadence so a   │
│  stuck-stale read can't masquerade as fresh to alarm comparators.│
│  Client lock released between retry attempts so a queued control │
│  write can pre-empt a degraded-link retry chain.                 │
│                                                                  │
│  State machine + control service:                                │
│   • semantic engine state (stopped/cranking/running/…)           │
│   • panel-mode tracking (AUTO/MANUAL/OFF) — gates remote writes  │
│   • two-step confirm tokens (8-char hex, 30 s TTL, single-use)   │
│   • server-side state-validity guards (409 invalid_state) —      │
│       re-checked INSIDE the lock after token consume             │
│   • server-side panel-mode guard      (409 panel_mode_locked)    │
│   • audit log on every command                                   │
│                                                                  │
│  Storage (SQLite WAL, synchronous=FULL — writes off event loop): │
│   • telemetry / telemetry_1m / telemetry_1h                      │
│   • events / alarms_active / audit / kv                          │
│   • retention task aggregates and prunes every 5 min             │
│                                                                  │
│  Slack notifier (per-(code,kind) dedupe within 60 s, retries    │
│  abandoned past 5 min so a sustained outage can't backlog).     │
└─────────────────────────────┬─────────────────┬─────────────────┘
                              │                  │ Modbus TCP (MBAP)
                              │ RTU over TCP     │ default 5020
                              │ 9600 8N1 slave 100│
                              v                  v
                       ┌──────────────┐   ┌──────────────────┐
                       │ Network      │   │ ATS-Pi companion │  optional
                       │ serial bridge│   │ (separate Pi +   │  ats.enabled
                       │ Lantronix /  │   │  ADAM-6060) —    │  in config
                       │ Moxa / ser2net│  │  ASCO transfer   │  Phases 1-3
                       └──────┬───────┘   │  switch sensing  │  software-
                              │           └──────────────────┘  complete;
                              │ RS-232 (9600 8N1)               live use needs
                              v                                 commissioning
                       ┌──────────────┐
                       │ H-100        │  Generac H-100 controller
                       │ controller   │  on the generator panel
                       └──────────────┘
```

### Reliability features

- **Hardware watchdog on pid 1** — drop-in at `/etc/systemd/system.conf.d/10-genwatch-hwwatchdog.conf` sets `RuntimeWatchdogSec=15s`. systemd pets the Pi's BCM2712 watchdog via `/dev/watchdog`; a kernel hang, USB controller wedge, or thermal panic hard-resets the Pi within ~15 s.
- **Software watchdog driven by a poll heartbeat** — `Type=notify` unit with `WatchdogSec=60s`. The app only pings `sd_notify(WATCHDOG=1)` while a *prime* Modbus poll has completed within the last ~6 × prime cadence. A deadlocked poll task (pymodbus stuck on a bad socket) lets systemd SIGKILL and restart. Uses a monotonic clock so NTP/DST jumps can't fool the timing. Cold-start grace capped at 5 minutes measured from actual service start (not from after poller setup completes).
- **TCP keepalive on the Modbus socket** — `SO_KEEPALIVE` + Linux `TCP_KEEPIDLE=30` / `KEEPINTVL=10` / `KEEPCNT=3`. The kernel drops a wedged socket (Lantronix reboot, NAT idle timeout, switch flap with no FIN/RST) within ~60 s instead of waiting for application read timeouts to exhaust.
- **Graceful degradation when the link is down** — a Modbus connect failure at startup no longer hard-exits. The service stays up with comms shown as `LOST` in the UI; the poller reconnects in the background. Stops systemd restart-thrash from burning the SD card during outages.
- **Batch-read fan-out, no sentinel zeros, per-register TTL** — a failing block read falls back to single-register reads so one bad address can't blank out an entire telemetry tier. Registers whose fan-out *also* fails are skipped (the previous value is kept) rather than overwritten with `0`. Each successful decode stamps a monotonic timestamp; values older than 3× their tier cadence are dropped from `reading.values` so a stuck-stale read can't masquerade as fresh forever (alarm comparators see `None` and degrade gracefully rather than evaluating against a phantom value).
- **Modbus client lock released between retry attempts** — a failing read no longer holds the wire lock across its backoff sleeps. A queued control write can pre-empt the retry chain, dropping worst-case Stop-command latency under degraded comms from ~5.8 s to one timeout (~1.7 s).
- **Register-map hot-reload** — `POST /api/registers/reload` re-derives the prime/base batch tables under a lock and swaps them into the live poller, state machine, and control service. The state machine snapshots its regmap reference at the top of each `update()` call so a reload landing mid-update can't produce torn derivations. Verified by `POST /api/registers/verify` (static + live read probe).
- **SQLite WAL with `synchronous=FULL`** — one persistent writer connection (no per-write connection/checkpoint churn), with telemetry and retention writes dispatched off the event loop via `asyncio.to_thread`. Reads use their own connections and don't take the write lock, so `/api/status`, history, and events queries never serialize behind a write or a multi-thousand-row prune. Prunes delete in bounded chunks and a periodic `wal_checkpoint(TRUNCATE)` keeps the WAL from growing without bound on the SD card.
- **Frontend stale-data indicator** — a red **STALE DATA** badge appears when the WebSocket is down or no live push has arrived in ~3 poll intervals, so operators don't act on frozen numbers. WebSocket reconnects with exponential backoff (cap 30 s). Confirm-modal countdown anchored to the operator's wall clock + server-provided TTL so client/server clock drift can't produce "valid -30 s" or infinite refresh loops.
- **Panel-mode freshness gate (FE)** — control buttons require `panel` to be in a *recent* WS snapshot, not just the boot seed. Defense against a backend that drops the field while keeping the WS alive.
- **Per-poll timeouts and retries** on every Modbus read; configurable in `config.yaml` (`modbus_tcp.timeout_s`, `modbus.retries`, `modbus.backoff_s`).
- **Comms watchdog** — declares LOST after no successful prime poll for 3× the prime cadence; emits a `comms` event over the WebSocket so the badge transitions live.
- **Two-step confirm tokens on every state-changing endpoint** — 8-char hex, 30 s TTL, single-use (`pop`-on-consume), operator-bound. Required on start / stop / exercise / transfer AND on alarm-ack. Every issue / consume / expiry / mismatch is audit-logged.
- **Server-side state validity + panel-mode gate (in-lock)** — every remote command re-checks `engine_state` and panel key-switch position *inside* the control lock, after token consume, before the Modbus write. Two concurrent control requests can't both observe a stale snapshot and both fire.
- **WebSocket Origin allowlist + periodic re-validation** — `/ws/live` validates `Origin` before `accept()` and re-decodes the session JWT every 60 s inside the message loop, so an expired or secret-rotated token can't keep a long-lived socket streaming.
- **ATS-Pi cross-check (when companion is enabled)** — raises `ATS_LOADSOURCE_DISAGREE` when the ATS-Pi position contradicts the H-100 electrical output for ≥3 consecutive polls. The `ats=generator + zero-output` arm is gated on `normal_available == False` so the normal ASCO retransfer-delay window doesn't trip a false positive on every utility-restore cycle.
- **ATS maintained-command edge visibility** — a warn event is written when the inhibit / force-transfer read-back changes without a matching GenWatch command write. Most importantly the companion's ICD §8.3 comms-loss auto-release: an operator who asserted Inhibit during maintenance sees that it dropped, instead of discovering it from a transfer they believed was inhibited. Also flags a companion restart or a foreign Modbus client driving the outputs.
- **Slack dedupe + retry deadline** — per-(code, raised/cleared) suppression within a 60 s window so a flapping alarm bit can't exhaust the 200-slot queue. Per-message wall-clock deadline (5 min) so a sustained Slack outage doesn't backlog stale events past the point newer alerts can get through.
- **Login rate-limiter** — token-bucket per source IP, 5 burst then 1 per 3 min. Returns `429` with `Retry-After`.
- **Retention** — raw telemetry pruned at 7 d, 1-min rollup at 90 d, 1-hour rollup at 730 d (raw → 1 m → 1 h aggregation so long-range history survives past the 1-minute horizon), info / ok events at 30 d. Alarms, warns, and the audit log are never auto-pruned.

---

## 12. Adapting the register map

The shipped `backend/genwatch/registers/h100.yaml` is derived from the [`jgyates/genmon`](https://github.com/jgyates/genmon/blob/master/genmonlib/generac_HPanel.py) project's `generac_HPanel.py` — a field-tested open-source H-100 integration. Addresses and bit meanings were cross-referenced against Generac's own [H-100 Control Panel Technical Manual (0F3750)](https://soa.generac.com/manuals/5309519/0F3750). If you're seeing wrong values on a real panel, the most likely causes (in order) are:

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

## 13. Development

### Local development (no hardware)

```bash
git clone https://github.com/SethMorrowSoftware/GenWatch.git
cd GenWatch

# Backend
cd backend
python3 -m venv .venv
# Prefer the lockfile for reproducible installs:
.venv/bin/pip install --require-hashes -r requirements.lock
# Or requirements.txt during a dep bump (then regenerate the lock — see §9):
# .venv/bin/pip install -r requirements.txt
# Dev-only tools (pytest etc.):
.venv/bin/pip install -r requirements-dev.txt
GENWATCH_MOCK=true \
GENWATCH_AUTH__JWT_SECRET=$(.venv/bin/python -m genwatch gensecret) \
GENWATCH_AUTH__ADMIN_PASSWORD_HASH=$(.venv/bin/python -m genwatch hash dev) \
.venv/bin/python -m genwatch serve
# → http://127.0.0.1:8000

# Frontend (separate terminal)
cd frontend
npm ci                    # honor package-lock.json — same as production install
npm run dev               # → http://127.0.0.1:5173 (proxies /api + /ws to :8000)

# Log in with password "dev"
```

The mock client simulates a plausible H-100 — engine state machine, electrical output, alarm injection. Control buttons drive the mock, so the full operator flow works without any hardware.

### Tests

```bash
cd backend
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests/ -v
```

232 tests across ten files:

- `test_registers.py` — YAML loader, decoder for every `RegType`, batch coalescing, address-overlap + bad-FC validation.
- `test_state_machine.py` — engine-state derivation, alarm-bit debounce, panel-mode decode.
- `test_alarm_filtering.py` — `min_poll_count` debounce + `suppress_in_states` filter behavior across raise/clear edge cases.
- `test_endtoend.py` — boots the app with the mock client, drives the full operator flow (login → confirm → start → state-validity rejection → panel-mode-locked rejection), verifies confirm-token on alarm-ack, CSRF blocks cross-origin POSTs, and that `/api/registers/reload` propagates to the live poller / state machine / control service.
- `test_hardening.py` — rate-limiter math, events retention, `sd_notify` parsing, transport selection, TCP keepalive socket options, poller heartbeat stamping (incl. withheld when the engine-state block fails), batch-fallback behavior, fan-out-failure preserves last-good value, per-tier TTL evicts stale values, short reads count as failures, a partial fan-out doesn't flip comms to LOST, Modbus client lock released between retry attempts, JWT-secret + admin-hash refuse-to-boot in production (empty / `REPLACE_ME` placeholder / too-short / non-bcrypt), control rejected when H-100 comms LOST, env-vars override `config.yaml` (with nested deep-merge), SQLite 1m→1h rollup + long-span read + chunked prune + WAL checkpoint, `panel` CLI command output (text + JSON), `genwatch hash` stdin-prompt behavior.
- `test_ats_control.py` — ATS-Pi command write side (Phase 3): each command writes the expected ICD register/value, confirm-token flow (valid / invalid / single-use), role gating (force-transfer admin-only), force-transfer healthy-utility override guard, comms-loss / non-authoritative link returns 502 / 409.
- `test_slack.py` — block builder, gating flags, dispatch worker, retry-on-transport-error vs no-retry-on-Slack-error, dedupe window suppresses flap, retry deadline abandons stale messages, token sanitization (never echoed to audit), hot-reload from `PUT /api/config`.
- `test_ats_service.py` — ATS-Pi snapshot decode, authoritative-gate, ICD §5.4 minor-version semantics, reboot detection.
- `test_ats_loadsource_precedence.py` — load-source derivation precedence (ATS-Pi authoritative vs H-100 fallback), `ATS_LOADSOURCE_DISAGREE` debounce + gating on `normal_available`, handover across comms-lost / comms-recovered windows.
- `test_ats_icd_alignment.py` — pins ICD doc ↔ YAML ↔ mock alignment so a register addition can't silently drift one of them.

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

design_handoff_genwatch/                  Original design spec — keep as
                                          reference for design tokens
                                          (colors, type, spacing) and
                                          screen-layout decisions when
                                          touching the UI. The CSS itself
                                          lives at frontend/src/styles/
                                          genwatch.css.

docs/integrations/                        Wire contract + integration plan
  ats-pi-icd.md                           ATS-Pi ↔ GenWatch wire contract.
                                          Companion repo reads this.
  ats-pi-plan.md                          Phased integration plan
                                          (Phases 1-3 shipped on the
                                          GenWatch side & validated against
                                          the mock; live ATS commands still
                                          gated on commissioning the hybrid
                                          ATS-Pi — Group-5 serial sense +
                                          ADAM-6060 command relays).
```

### API contract

Every `/api/*` endpoint requires authentication except `/api/health` (a deliberately-anonymous liveness probe for external uptime monitoring — returns only `{ok, mock}` to anon callers) and the auth endpoints themselves. Mutating endpoints additionally require the request's `Origin`/`Referer` to match the host (or `cors_origins` allowlist) — the CSRF middleware rejects with 403 `csrf_blocked` otherwise. Deploying outside a trusted LAN still wants Tailscale / Caddy / firewall ACLs per [§8](#8-security-recommendations) — auth + CSRF are defense in depth, not a substitute for network isolation.

| Method | Path                                          | Auth   | Notes                                                       |
|--------|-----------------------------------------------|--------|-------------------------------------------------------------|
| GET    | `/api/health`                                 | public | Anon callers see `{ok, mock}` only. Authed callers get comms state, uptime, version, DB size. |
| POST   | `/api/auth/login`                             | public | `{ password }` → session cookie (rate-limited per IP)       |
| POST   | `/api/auth/logout`                            | public | Clear cookie                                                |
| GET    | `/api/auth/me`                                | public | Identity (200 with `{authenticated: false}` when anonymous) |
| GET    | `/api/status`                                 | op+    | Full live snapshot (engine, comms, reading, panel, alarms, ATS) |
| GET    | `/api/telemetry`                              | op+    | `?metric=&from=&to=&max_points=` (server-side decimation)   |
| GET    | `/api/telemetry/columns`                      | op+    | Available telemetry metric names                            |
| GET    | `/api/columns`                                | op+    | Register-name → DB column mapping                           |
| GET    | `/api/events`                                 | op+    | `?limit=&severity=alarm,warn&type=&from=&to=`               |
| GET    | `/api/alarms?active=true`                     | op+    | Currently-active alarms                                     |
| POST   | `/api/alarms/{code}/ack`                      | op+    | Body `{ confirm_token }`; writes `0x0001` → `0x012E`        |
| GET    | `/api/alarm-codes`                            | op+    | Static alarm-code reference table from the YAML             |
| GET    | `/api/control/confirm`                        | op+    | Issue 8-char hex confirm token (30 s TTL, single-use)       |
| POST   | `/api/control/{start,stop,exercise,transfer}` | op+    | Body `{ confirm_token }`; 409 on invalid state, panel ≠ AUTO, or comms LOST |
| POST   | `/api/ats/test`                               | op+    | ATS-Pi momentary test transfer; body `{ confirm_token }` (ATS Phase 3) |
| POST   | `/api/ats/inhibit`                            | op+    | Body `{ confirm_token, assert }` — assert/release inhibit (maintained) |
| POST   | `/api/ats/force-transfer`                     | admin  | Body `{ confirm_token, assert, override }`; 409 unless `override` while utility available |
| POST   | `/api/ats/bypass-delay`                       | op+    | ATS-Pi momentary bypass of the transfer time delay          |
| GET    | `/api/config`                                 | op+    | Effective config (bot_token + jwt_secret never returned)    |
| PUT    | `/api/config`                                 | admin  | Update on-disk config; Slack hot-reloads, others need restart |
| POST   | `/api/slack/test`                             | admin  | Send a synchronous test message; returns `{ok, detail}`     |
| GET    | `/api/registers`                              | op+    | Current register map + last-read values for each            |
| POST   | `/api/registers/reload`                       | admin  | Re-parse YAML, propagate to live poller + state + control   |
| GET    | `/api/registers/verify`                       | admin  | Static + live read verification (skipped in mock mode)      |
| WS     | `/ws/live`                                    | cookie | Cookie auth + Origin allowlist + 60 s periodic re-validation. Pushes `hello` / `snapshot` / `transition` / `alarm` / `alarm-cleared` / `comms` / ATS events / `ping`. |

Roles: **viewer**, **operator**, **admin** — `require_operator` admits `{operator, admin}`, `require_admin` admits `{admin}`. Today only one login exists (the `admin_password_hash` account, issued `role="admin"`), so both gates pass for every authenticated user. The distinction is forward-compat scaffolding for a future second password; pick the gate that documents intent (admin for secret-handling, operator for operational reads/writes) rather than treating `require_admin` as a real privilege boundary until the split lands.

All errors return JSON `{ detail: { code, message } }` with appropriate HTTP status. Common error codes:

| Status | Code                  | Cause                                                            |
|--------|-----------------------|------------------------------------------------------------------|
| 400    | `token_invalid`       | Confirm token missing, expired, or already consumed              |
| 400    | `token_expired`       | Confirm token's 30 s TTL elapsed                                 |
| 401    | `unauthorized`        | No / invalid session cookie                                      |
| 403    | `forbidden`           | Role insufficient for the action                                 |
| 403    | `token_mismatch`      | Confirm token was issued to a different operator                 |
| 403    | `csrf_blocked`        | Mutating request whose `Origin`/`Referer` is not in the allowlist |
| 409    | `invalid_state`       | Control verb not valid for current engine state (e.g. start while running) |
| 409    | `panel_mode_locked`   | Panel key switch is MANUAL / OFF / unknown — remote writes blocked |
| 429    | `rate_limited`        | Too many login attempts; `Retry-After` header gives the wait    |
| 502    | `modbus_failed`       | Underlying Modbus write returned an error                        |

---

## 14. License

MIT — see [LICENSE](LICENSE).
