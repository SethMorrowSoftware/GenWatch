# GenWatch

Raspberry Pi monitoring & control dashboard for the **Generac H-100** industrial generator over Modbus RTU.

A single-pane operator console: live engine state, electrical output, two-step-confirm controls (start / stop / quiet-test / transfer), time-series history, events & alarms, and on-Pi configuration of the serial port, register map, and retention policy.

> **Status:** MVP — runs end-to-end on a Pi 4 with a USB-RS485 adapter pointed at the H-100's Modbus port. Backend has unit + integration tests against a synthetic slave; frontend is a typed React build of the design handoff.

---

## What's in the box

```
backend/      FastAPI service + pymodbus poller + SQLite + WebSocket
  genwatch/
    modbus/          register YAML loader, decoder, RTU client, two-tier poller
    services/        state machine, control flow, auth, retention
    api/             REST + WebSocket routes
    registers/       h100.yaml — default register map (overridable)
  tests/             pytest (20 tests: register decode, batching, e2e mock)

frontend/     Vite + React 18 + TypeScript
  src/
    api/             typed client
    hooks/           useLiveData (WS + status seed + reconnect backoff)
    components/      Icon, Pill, Sparkline, LineChart, Card, Modal, Switch
    views/           LiveView, HistoryView, EventsView, SettingsView, LoginView, ConfirmModal
    styles/          genwatch.css (verbatim from the design handoff)

deploy/
  systemd/genwatch.service   Hardened unit with watchdog + restart-always
  scripts/install.sh         Idempotent installer for Raspberry Pi OS
  config.yaml.example        Annotated config template

design_handoff_genwatch/     Original prototype + design system docs (reference)
screenshots/                 Reference visuals from the handoff
```

---

## Quick start — development (no hardware)

```bash
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

# Login with the password you set (`dev` above).
```

The mock client simulates a plausible H-100 — engine state machine,
electrical output, alarm injection. The control buttons drive the mock
state, so the full operator flow works without an RS-485 adapter.

---

## Install on a Raspberry Pi 4

Tested on Raspberry Pi OS Bookworm (64-bit). A USB-to-RS485 adapter
(e.g. FTDI FT232 + MAX485 module) connected to the H-100 controller's
Modbus port. Wiring:

```
   Pi USB-RS485             H-100 controller
   ─────────────             ─────────────
   A (D+)        ───────►   A / TX+ / D+
   B (D−)        ───────►   B / TX− / D−
   GND           ───────►   COM / GND
                            120 Ω termination at the far end of the bus
```

Build the frontend on a dev machine (or on the Pi if you have node 20+):

```bash
cd frontend && npm install && npm run build
```

Then on the Pi:

```bash
git clone https://github.com/sethmorrowsoftware/genwatch.git /tmp/genwatch
cd /tmp/genwatch
sudo deploy/scripts/install.sh
```

The installer will:

1. Create the `genwatch` system user (in `dialout` for serial access)
2. Build a venv at `/opt/genwatch/venv` and install the backend
3. Copy `frontend/dist/` to `/usr/share/genwatch/ui/`
4. Provision `/etc/genwatch/config.yaml` with a random `jwt_secret`
5. Install + enable the systemd unit and start the service

Then hash an admin password and paste it into the config:

```bash
sudo /opt/genwatch/venv/bin/python -m genwatch hash 'a-strong-password'
sudo nano /etc/genwatch/config.yaml          # paste into admin_password_hash
sudo systemctl restart genwatch
```

Browse to `http://<pi-ip>:8000`. The cookie-based session lasts 12 h by default.

---

## Architecture

### Modbus polling

Two tiers — state is checked fast, telemetry is checked slowly.

| Tier   | Registers                                | Default cadence |
|--------|------------------------------------------|-----------------|
| prime  | `engine_state`, `alarm_state`, `switch_state` | every 1.5 s     |
| base   | RPM, voltages, currents, frequency, kW, oil, coolant, battery, run-hours, fuel | every 15 s |

The poller coalesces contiguous registers into single Modbus reads (the
default H-100 map collapses 15 base reads into 3 batches). Comms health
is a rolling 60-poll success window; a watchdog drops state to LOST if
no prime poll succeeds for 3× the prime cadence.

### Register map

Configurable YAML — see [`backend/genwatch/registers/h100.yaml`](backend/genwatch/registers/h100.yaml). Includes:

- engineering-units `scale` per register (raw `139` → `13.9 V`)
- big-endian `u32` for accumulators (run hours, start count)
- `engine_state_map` raw-int → semantic state name
- `alarm_codes` table — code → description + severity
- `controls` block for FC06 write-gated commands

Edit it in place; reload without restart via `POST /api/registers/reload` (admin).

### Control flow

```
operator → click "Remote Start"
        ← UI calls GET  /api/control/confirm  →  random 8-char hex token (TTL 30 s)
        → UI displays token, requires explicit acknowledgement checkbox
        → POST /api/control/start  { confirm_token }

server: validates session → validates role (operator+) → consumes token
        → validates engine_state is in {stopped} for "start"
        → writes Modbus FC06 to 0x00A0 = 1
        → audit-logs operator + action + token + result
        → next poll catches the resulting state transition
```

Tokens are single-use, audit-logged on issue/consume/evict, and tied to
the issuing operator. State-validity is enforced server-side regardless
of what the UI does.

### Storage

SQLite in WAL mode at `/var/lib/genwatch/db.sqlite`. Schema:

- `telemetry` — wide row per base poll (one column per metric)
- `telemetry_1m`, `telemetry_1h` — rollup tables computed every 5 min
- `events` — append-only severity + type + message + meta
- `alarms_active` — current alarms; cleared on alarm-state→0 or operator ack
- `audit` — append-only command log (every confirm-token + control write)
- `kv` — small key/value table for state that should survive restarts

Retention runs every 5 min: aggregate the last hour into 1-min buckets,
then prune raw older than `raw_days` and 1-min older than `rollup_1m_days`.

### Auth

Single bcrypt-hashed admin password in `config.yaml`. JWT cookie session
(`HS256`, 12 h default). The WebSocket accepts the same cookie. Set
`auth.jwt_secret` at install time — the installer generates one for you;
without it the service generates an ephemeral secret per restart so
tokens don't survive a restart.

### Reliability

- systemd unit: `Restart=always`, `WatchdogSec=120`, exponential start-limit backoff
- DB: WAL journal, NORMAL sync, 8 MiB cache; survives Pi power loss without corruption
- Modbus: per-request timeout + retries with backoff; comms watchdog
- Token replay protection: single-use, 30 s TTL, audit-logged
- Server-side state-validity for every control command
- Pi 4 process hardening: `NoNewPrivileges`, `ProtectSystem=strict`, `MemoryMax=512M`

---

## Adapting the register map for your firmware

The shipped `h100.yaml` mirrors the addresses documented in the design
handoff and matches typical H-100 / HSB implementations. **Your real
firmware revision may use different addresses.** To find them:

```bash
# Read a sweep of 16 holding registers starting at 0x0001
sudo -u genwatch /opt/genwatch/venv/bin/python -m genwatch modbusdump \
  --addr 0x0001 --count 16

# Common probes for an unknown panel
for a in 1 16 32 48 64 160; do
  sudo -u genwatch /opt/genwatch/venv/bin/python -m genwatch modbusdump \
    --addr $a --count 8
done
```

When you have the right addresses, edit
`/opt/genwatch/genwatch/registers/h100.yaml` and reload:

```bash
curl -X POST http://localhost:8000/api/registers/reload \
  -H "Authorization: Bearer $(cat ~/.genwatch.token)"
```

---

## API contract

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
| GET    | `/api/registers`              | Current register map + last read   |
| POST   | `/api/registers/reload`       | Re-read YAML from disk (admin)      |
| WS     | `/ws/live`                    | `snapshot` / `transition` / `alarm`  |

All errors return JSON `{ detail: { code, message } }` with appropriate HTTP status.

---

## Tests

```bash
cd backend
.venv/bin/python -m pytest tests/ -v
# 20 tests: register decode/scale/batch, e2e with mock client (status,
# health, auth-required, full control flow, single-use tokens,
# state-validity enforcement)
```

---

## License

MIT — see [LICENSE](LICENSE) (add one before shipping).
