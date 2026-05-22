# Design Spec: GenWatch — Generac H-100 Operator Console

> **Status: implemented.** This document was the original design handoff. The
> backend lives in `backend/`, the React + TypeScript frontend in `frontend/`,
> and the Pi deployment in `deploy/`. Keep this file as a **spec reference**
> for design tokens (color, type, spacing), screen layouts, and the API
> contract — useful when adding new views or auditing visual regressions.
> The prototype JSX/HTML files referenced below have been removed; the CSS
> they defined is preserved verbatim in `frontend/src/styles/genwatch.css`.

## Overview

GenWatch is a Raspberry-Pi-hosted monitoring & control dashboard for a Generac H-100 industrial generator over Modbus RTU. The operator console has four views (Live, History, Events & Alarms, Settings) and a confirm-token-gated control surface for start / stop / exercise / transfer commands.

This README documents the **UI design**; the backend implementation is documented in the repo-root `README.md`.

## Fidelity: High

Pixel-perfect colors, typography, spacing, and interaction states. The shipped frontend mirrors them exactly via `frontend/src/styles/genwatch.css` (the original prototype CSS, unchanged).

---

## Recommended stack

Matches the spec's recommendation:

- **Frontend:** Vite + React + TypeScript, static build served by FastAPI
- **Charts:** uPlot (lightweight, performant on a Pi) or Chart.js. The prototype uses hand-rolled SVG which is fine to keep for the small sparklines, but switch to uPlot for the History view's large chart.
- **Icons:** inline SVGs (see `frontend/src/components/primitives.tsx` `Icon`). The implementation kept the inline approach over `lucide-react` to save ~20 KB on the Pi bundle; if you'd rather swap, every icon has a 1:1 Lucide equivalent (see §Icons).
- **Fonts:** Geist (sans) + JetBrains Mono — both via Google Fonts or self-hosted woff2. Geist is recent; if it's not in the codebase, add it.
- **State:** local `useState` + a single WebSocket subscription is enough; no Redux needed. TanStack Query is a good fit for the REST calls (events, history, settings).

---

## Design tokens

All tokens are CSS custom properties on `:root`. The light theme is a sibling `:root[data-theme="light"]` block. **Use these exact values.**

### Color — dark (default)
| Token       | Value      | Use |
|-------------|------------|-----|
| `--bg`      | `#07090d`  | App background |
| `--bg-1`    | `#0c0f15`  | Subtle alt background |
| `--panel`   | `#11151c`  | Card surface |
| `--panel-2` | `#161b24`  | Inset / input surface |
| `--panel-3` | `#1c222d`  | Hover / pressed surface |
| `--border`  | `#1f2530`  | Default border |
| `--border-2`| `#2a313e`  | Stronger border (focus, primary buttons) |
| `--text`    | `#e6ebf2`  | Primary text |
| `--text-2`  | `#a3aab6`  | Secondary text |
| `--text-3`  | `#6b7280`  | Muted / labels |
| `--text-4`  | `#4a525e`  | Faint / disabled |

### Color — light
| Token       | Value      |
|-------------|------------|
| `--bg`      | `#f4f5f7`  |
| `--bg-1`    | `#ebedf1`  |
| `--panel`   | `#ffffff`  |
| `--panel-2` | `#f7f8fa`  |
| `--panel-3` | `#eef0f4`  |
| `--border`  | `#e5e7ec`  |
| `--border-2`| `#d6dae2`  |
| `--text`    | `#14171d`  |
| `--text-2`  | `#4b5260`  |
| `--text-3`  | `#6b7280`  |
| `--text-4`  | `#9aa1ac`  |

### Status palette (oklch — preserve across both themes)
| Token (dark)  | Value                       | Used for |
|---------------|-----------------------------|----------|
| `--green`     | `oklch(0.80 0.16 148)`      | Running, OK, transfer-on-gen |
| `--green-d`   | `oklch(0.50 0.14 148)`      | Green borders/strokes |
| `--amber`     | `oklch(0.82 0.15 75)`       | Exercising, warn, out-of-band |
| `--amber-d`   | `oklch(0.55 0.13 75)`       | Amber borders |
| `--red`       | `oklch(0.70 0.22 25)`       | Alarm, danger button |
| `--red-d`     | `oklch(0.45 0.18 25)`       | Red borders |
| `--blue`      | `oklch(0.78 0.13 220)`      | Comms, transfer-on-utility, info |
| `--blue-d`    | `oklch(0.45 0.12 220)`      | Blue borders |
| `--slate`     | `oklch(0.70 0.02 250)`      | Stopped (neutral) |

Light theme rebalances chroma; see the `[data-theme="light"]` block in `frontend/src/styles/genwatch.css`. Always go through `color-mix(in oklch, var(--<status>) <N>%, var(--panel-2))` for tinted backgrounds rather than hardcoding new colors.

### Typography
- **Sans:** Geist, fallback `ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif`. Weights 400 / 500 / 600 / 700.
- **Mono:** JetBrains Mono, fallback `ui-monospace, SFMono-Regular, Menlo, monospace`. Weights 400 / 500 / 600.
- **Tabular numerals** (`font-variant-numeric: tabular-nums`) on every numeric value — both mono and sans contexts.
- **Scale:**
  - 11 px / 600 / `letter-spacing: 0.09em` / uppercase — card titles, eyebrow labels
  - 11.5–12 px — pill chips, comms badge
  - 13–13.5 px — body / table cells / row text
  - 15 px / 600 — brand name
  - 17 px / 600 — modal heading
  - 22 px / 600 / `-0.015em` — page titles
  - 26–28 px / 500 mono — metric values
  - 56 px / 600 / `-0.025em` — hero state title ("Running")

### Spacing scale
The CSS uses `--gap: 14px` (regular density), `10px` (compact), `18px` (comfy). Internal card padding is `12px 14px` (head) / `14px` (body). Hero panel padding `22px`. Use this scale everywhere — do not introduce new spacing values.

### Radii
- `--rad-s: 6px` — chips, small inputs
- `--rad: 10px` — cards
- `--rad-l: 14px` — modal
- `999px` — pills, badges, switches

### Shadows
- `--shadow-sm: 0 1px 0 rgba(255,255,255,0.02) inset, 0 1px 2px rgba(0,0,0,0.3);` — card lift
- `--shadow: 0 1px 0 rgba(255,255,255,0.03) inset, 0 6px 24px rgba(0,0,0,0.35);` — modal

### Status borders / tinted backgrounds
Always derived: `1px solid color-mix(in oklch, var(--<status>) 35%, var(--border))` and `background: color-mix(in oklch, var(--<status>) 8–14%, var(--panel-2))`.

---

## Screens / views

There are four primary views switched by the top nav (radio-pill control). State `view ∈ {live, history, events, settings}`.

### 1. Live

The default view. Shows current generator state, electrical output, engine internals, controls, and recent events.

**Layout:** vertical stack of rows inside a max-width 1480 px main container with 22 px padding.

1. **Page head** — title "Site overview" + sub "SITE-23 · Generac H-100 · 200 kW · Cummins QSB7-G5 · last sync 1.5 s ago", right-aligned status pills ("Auto · 03:00 Sun exercise", "2 HTS · 0 annunciators").
2. **Optional alarm strip** (full-width, red-tinted) — shown only when an alarm is active. LED + code + description + Active pill + "Acknowledge" danger button (right-aligned via margin-left: auto).
3. **Status hero** (full-width, 1 fr / 1.2 fr grid):
    - **Left half:** state badge ("ON LOAD" pill, colored by state) → big 56 px state title ("Running") → sub "On load · Utility lost · 04:23:21 (time-in-state, mono)" → 3-up hero-stats strip (Frequency / Real power / Engine RPM, mono values @ 18 px).
    - **Right half:** ATS Transfer Switch panel — 320×120 SVG diagram (Utility box → ATS contact box → Load box, with active line in green when on-generator). Below the diagram: 3-up sub-stats (Load %, Last transfer, Transfers 30d).
    - Hero left has a radial glow tinted by the current state color (running → green, alarm → red, exercising → amber).
4. **Row: Electrical (2/3) + Controls (1/3)**
    - **Electrical card** ("Generator Output · 3-phase · Line-to-Line"): two columns. Left = Voltage L–L with three phase rows (A–B / B–C / C–A) showing label · proportional bar · mono value + sparkline strip. Right = Current per phase (A / B / C). Below, a separator and three BigMetric tiles: Frequency / Real power / Apparent (each with sparkline).
    - **Controls card** ("Operator · two-step confirm"): vertical stack of 4 `.ctl-btn` buttons (Remote Start / Remote Stop / Quiet-Test / Transfer back). Each button: 32 px tinted icon square · two-line label+desc · keyboard hint chip. Disabled state opacity 0.45. State validity rules (see §Controls). Footer: lock-icon caption "Commands write to 0x00A0–A2 via FC06. Engine hardware safeties (panel) remain primary."
5. **Row: Engine (2/3) + Tank/Maintenance (1/3)**
    - **Engine card** ("Cummins QSB7-G5 · 200 kW"): 4-up grid of EngineMetric tiles (RPM, Oil pres., Coolant, Battery). Each tile: label + warn-range hint, big mono value, range bar OR sparkline (toggled by tweak), tinted by metric color (green/blue/amber/text-2).
    - **Tank & Maintenance card**: fuel bar (% + ~gal, green→amber gradient, red when <25 %), with tick marks at every 10 %, and a kv list (Run hours, Since last service, Next service due, Next exercise, Last alarm).
6. **Recent events** (full-width) — compact `EventsFeed` showing 6 latest events; each row: relative time (mono) · severity dot · type tag · description · meta.
7. **Footer** (sticky-feel, but in normal flow): "GenWatch · running on raspberry-pi-4 · uvicorn 0.32 · pymodbus 3.7 · python 3.11" left, "/dev/ttyUSB0 · 9600 8N1 · slave 100 (0x64) · 1,847.6 h" right (mono).

**Engine-state visual map:**
| State        | Title shown   | Hero accent | Pills shown    | Sub                                          |
|--------------|---------------|-------------|----------------|----------------------------------------------|
| `stopped`    | Stopped       | slate       | "STOPPED"      | AUTO · Ready                                 |
| `cranking`   | Cranking      | amber       | "CRANKING"     | Engine start in progress                     |
| `running`    | Running       | green       | "ON LOAD"      | On load · Utility lost                       |
| `exercising` | Exercising    | amber       | "EXERCISING"   | Quiet-Test · No load                         |
| `cooling`    | Cooling       | amber       | "COOLING"      | Engine cool-down · 5:00 left                 |
| `alarm`      | Alarm         | red         | "ALARM"        | Shutdown · Operator action required          |

### 2. History (Telemetry History)

Time-series of any single metric, plus two summary cards.

- **Page head:** title "Telemetry History" + sub "SQLite time-series · raw 7 d, 1-min rollup 90 d · 8 indexed metrics" · right side pills ("84.1 MB · ↓6.4 KB/s") + Export-CSV button.
- **Main chart card** (flush):
  - Toolbar: range pill group `[10M, 1H, 6H, 24H, 7D, 30D]` (`aria-current="page"` on active) + range display ("09:33 — 10:33") · legend on right (color dot + metric name).
  - Metric tabs row: 8 chips for `kw / rpm / hz / oilP / coolT / batt / vAB / iA`. Each chip has a small colored swatch dot.
  - SVG line chart, 320 px tall, area gradient under the line, 5 horizontal grid lines with mono y-axis tick labels, 8 x-axis labels. Uses the metric's color.
  - Stat tiles row (min · avg · max · now), each in its own column with right border, mono values. "Now" tile shows the LIVE · {pollRate} dot.
- **Two-column row:**
  - **Daily energy card** — vertical bar chart, 14 bars, amber, varying opacity by value.
  - **Runtime breakdown card** — donut chart, 4 segments (Standby/On load/Exercise/Cool-down) with totals/percentages in a side list. Center reads "612.4 HOURS · 30D".

### 3. Events & Alarms

- **Page head:** title + sub "{n} of {N} events · {m} active alarm" · Saved-views + Export buttons.
- **Active alarm strips** (one per active alarm): same as Live's alarm strip.
- **Events log card** (flush):
  - Filter bar: severity chips (Alarm/Warn/Info/OK, all toggle on/off via `aria-pressed`) · type chips (ALL + each unique event type). Selected severity chips have the panel-3 background; selected type chip likewise.
  - Event rows: 5-column grid `90px / 16px / 90px / 1fr / 110px` = time (mono, text-3) · severity dot · type tag (mono uppercase text-3) · message (with `<em>` callouts for state names and entity names) · meta (mono, text-3, right-aligned).
- **Alarm codes reference table** — separate card. Standard data table: Code (mono) · Description · Severity (pill) · Action. Add-code button in card header.

### 4. Settings

Two-column layout (220 px sidebar + content), responsive to single column under 1080 px.

- **Page head:** title + sub + Discard / Save buttons.
- **Sidebar:** 6 items — Serial Port / Modbus / Register Map / Notifications / Users & Access / Retention. Selected has panel background + border.
- **Sections** (panels with head + table of `field-row`s):
  - **Serial Port:** Device path · Baud · Parity·Stop·Data (3-up selects) · Timeout · Connection test (Run-modbusdump button + "last test 8s ago · 12/12 ok" pill).
  - **Modbus:** Slave address · Register map file path · Read function code (0x03 vs 0x04) · Prime poll interval (ms) · Base poll interval (ms).
  - **Register Map:** full table of the YAML register file. Grouped rows ("Prime poll · 1.5 s", "Base poll · 15 s", "Controls · write-gated") render as full-width grey group headers; data rows show Address · Name · FC · Type · Scale · Unit · Last read.
  - **Notifications:** five channels (MQTT / Email / SMS / Push / SNMP), each as a field-row with Configure-link + Switch toggle.
  - **Users & Access:** 3-row user table (Name · Email · Role pill · Last seen · Edit). Invite-user button. Tailscale ACL indicator.
  - **Retention:** raw / 1-min rollup / 1-hour rollup retention numbers + disk usage bar.

### 5. Confirm modal

Triggered by every control button on Live. 440 px wide, blurred scrim.

- Header: "Confirm Remote Start" (or Stop / Quiet-Test / Transfer Back) + sub "Two-step confirm · audit-logged · operator: kim.harris"
- Body: 3 bullet items (`check-line.on`, green check) describing exactly what will happen — copy is **command-specific**:
  - **start:** "Engine will crank within 2 seconds" / "HTS-1 will transfer to GENERATOR" / "Run hours will accumulate"
  - **stop:** "HTS-1 will transfer back to UTILITY" / "Engine enters 5-minute cool-down" / "Site briefly on utility-only"
  - **exercise:** "Engine runs unloaded for 30:00" / "No transfer · utility remains primary" / "Sound profile: quiet mode"
  - **transfer:** "HTS-1 → UTILITY" / "Engine continues running through cool-down" / "Brief 100-200 ms power gap on load"
- One unchecked `check-line`: "I understand this will physically affect the generator and load." with desc "Hardware safeties at the H-100 panel remain primary." — toggles on click.
- A confirm-token box (small inset panel, mono token rendered after ~320 ms delay) — simulates the `GET /api/control/confirm` step.
- Footer: Cancel (ghost) + primary Action button. The Action is disabled until `confirmed && tokenLoaded`. Stop is `btn-danger`; others are `btn-primary`.

---

## Components inventory

All defined in `frontend/src/components/primitives.tsx` and `frontend/src/views/*.tsx`:

- `<Icon name size stroke>` — inline SVG library (~24 icons). **Map to lucide-react** in the real codebase; see §Icons.
- `<Pill tone>` — chip with leading dot. Tones: `ok | warn | alarm | info` (plus neutral default).
- `<Sparkline points width height color fill smooth strokeWidth>` — smooth single-line SVG sparkline with gradient fill area.
- `<LineChart series colors xLabels height>` — full chart (used in History). Smooth quadratic Bézier, area gradient, gridlines, axis labels.
- `<Gauge value min max color size strokeWidth>` — 270 ° radial gauge (declared but unused in current views; keep for future).
- `<Card title sub actions flush tight>` — primary container.
- `<Modal open onClose title sub footer>` — fixed-positioned modal w/ backdrop-blur scrim.
- `<LiveTick rateMs>` — green-dot blink + "LIVE · X.Xs" text.
- `<Switch value onChange>` — 36×20 toggle, green when on.
- `<StatusHero state reading timeInState>` — the hero block. See §Live #3.
- `<AtsPanel state reading>` — HTS-1 transfer switch diagram block.
- `<PhaseRow label value unit pct color>` — single phase row (used 3× per electrical sub-column).
- `<ElectricalCard reading series sparks>`
- `<EngineCard reading series sparks>` + `<EngineMetric>` + `<RangeBar>`
- `<ControlsPanel state onCommand>`
- `<FuelMaintCard reading>`
- `<EventsFeed limit dense>`
- `<ConfirmModal command onClose onConfirm>`
- `<Bars data colors>` / `<Donut segments>` — for History summary cards.

### Icons map (lucide-react equivalents)

| `name` in prototype | lucide-react name |
|---------------------|-------------------|
| activity            | `Activity`        |
| bolt                | `Zap`             |
| gauge               | `Gauge`           |
| bell                | `Bell`            |
| history             | `History`         |
| settings            | `Settings`        |
| play                | `Play`            |
| stop                | `Square`          |
| refresh             | `RefreshCw`       |
| chevron             | `ChevronRight`    |
| check               | `Check`           |
| x                   | `X`               |
| arrow               | `ArrowRight`      |
| switch_             | `ToggleRight` or custom |
| lock                | `Lock`            |
| user                | `User`            |
| filter              | `SlidersHorizontal` |
| download            | `Download`        |
| plus                | `Plus`            |
| flame               | `Flame`           |
| drop                | `Droplet`         |
| spark               | `Sparkles`        |
| wave                | `Waves`           |
| cable               | `Cable`           |
| cpu                 | `Cpu`             |
| bookmark            | `Bookmark`        |
| folder              | `Folder`          |
| list                | `List`            |
| plug                | `Plug`            |
| bars                | `BarChart3`       |

---

## Interactions & behavior

- **Top nav:** click a button → set `view` state → render that view. Active button has aria-current="page" and a green dot.
- **Comms badge:** real-time. Pulse animation tied to comms state (healthy=2.4s slow pulse, degraded=1.4s amber, lost=0.8s red). Percentage updates on each poll. Show last-good time when lost.
- **Live polling:** the prototype uses a `setInterval(seed++, rateMs)` loop. In production:
  - REST `GET /api/status` on mount.
  - Open WebSocket `/ws/live` → push updates on each prime poll. Each message updates the reading; merge into a rolling sparkline buffer (last ~40 points client-side).
  - State-transition messages also arrive over the WS — they immediately re-render the hero and (if alarm) the alarm strip.
- **Tweaks panel:** the floating "Tweaks" panel is a **prototype-only** affordance. Strip it from production.
- **Controls flow:**
  1. User clicks a `.ctl-btn`. If disabled by state validity, do nothing.
  2. `ConfirmModal` opens. Backend `GET /api/control/confirm` fetched; token displayed when received.
  3. User checks the acknowledgement box. Action button enables only when `token && confirmed`.
  4. On click, `POST /api/control/<verb>` with `{confirm_token: token}`. Backend validates token freshness + state validity + auth + audit-logs the request.
  5. Modal closes. Hero state badge transitions on the next WS push (do **not** optimistically transition — wait for the real state).
- **State validity** (what's clickable when):
  | State        | Start | Stop | Exercise | Transfer |
  |--------------|-------|------|----------|----------|
  | stopped      | ✅    | ❌   | ✅       | ❌       |
  | cranking     | ❌    | ✅   | ❌       | ❌       |
  | running      | ❌    | ✅   | ❌       | ✅       |
  | exercising   | ❌    | ✅   | ❌       | ❌       |
  | cooling      | ❌    | ✅   | ❌       | ❌       |
  | alarm        | ❌    | ✅   | ❌       | ❌       |
- **History controls:** clicking a range or metric chip refetches `GET /api/telemetry?metric=&from=&to=&res=` and re-renders the chart. Keep the previously-displayed series visible until the new one arrives (no spinner — show a subtle opacity fade on the line).
- **Events filters:** purely client-side over the loaded events page.
- **Settings save:** `Save & reload poller` → `PUT /api/config` with the full validated config. On 200, briefly disable the save button + show a toast "Poller restarted in 1.2 s". On schema error, inline-highlight the field(s).
- **Hover & focus:**
  - Buttons: `+1 px translateY` (chips), or background shift from `--panel-2` → `--panel-3` (control buttons).
  - Inputs on focus: `border-color: --border-2` + `0 0 0 2px color-mix(in oklch, var(--accent) 20%, transparent)` outer ring.
  - Table rows: `background: color-mix(in oklch, var(--accent) 3-4%, var(--panel))` on hover.
- **Responsive:** at ≤ 1080 px, hero collapses to single column, 4-up grids → 2-up, settings sidebar becomes a horizontal scroller. Below ~640 px, target single-column everything.

---

## State management

- `view: 'live' | 'history' | 'events' | 'settings'`
- `engineState: 'stopped' | 'cranking' | 'running' | 'exercising' | 'cooling' | 'alarm'` — driven by WS pushes.
- `reading: { rpm, hz, kw, oilP, coolT, batt, vAB, vBC, vCA, iA, iB, iC, fuelPct }` — current snapshot.
- `series: reading[]` — rolling buffer (40 points) for sparklines.
- `commsHealth: { state: 'healthy'|'degraded'|'lost', successPct, lastGoodAt, rateMs }`
- `alarms: ActiveAlarm[]`
- `events: Event[]` — paginated.
- `confirmCmd: 'start'|'stop'|'exercise'|'transfer'|null` — controls modal open state.
- `user: { name, role: 'viewer'|'operator'|'admin' }` — gates the Controls card visibility.
- Settings: serial, modbus, notif (all the field values shown).

---

## API contract (mirror this on the backend; the spec under §9 is the source of truth)

### REST
```
GET  /api/status            → { state, reading, comms, timeInState, activeAlarms, hts }
GET  /api/telemetry?metric=&from=&to=&res=
GET  /api/events?from=&to=&type=&sev=
GET  /api/alarms?active=true
GET  /api/config
PUT  /api/config
GET  /api/control/confirm   → { token, expiresAt }     (operator+)
POST /api/control/start     { confirm_token }          (operator+)
POST /api/control/stop      { confirm_token }
POST /api/control/exercise  { confirm_token, duration? }
POST /api/control/transfer  { confirm_token }
POST /api/auth/login
POST /api/auth/logout
```

### WebSocket
`/ws/live` — server pushes:
```ts
type LiveMessage =
  | { type: 'snapshot', reading: Reading, state: EngineState, comms: CommsHealth, ts: number }
  | { type: 'transition', from: EngineState, to: EngineState, ts: number }
  | { type: 'alarm', code: string, desc: string, severity: 'alarm'|'warn', ts: number }
  | { type: 'alarm-cleared', code: string, ts: number }
  | { type: 'event', sev: Severity, type: string, msg: string, meta: string, ts: number };
```

Snapshots fire on every prime poll (~1.5 s). Transitions and alarms fire immediately when detected.

---

## Implementation map

| Spec section            | Lives in                                                  |
|-------------------------|-----------------------------------------------------------|
| Design tokens (color, type, spacing, radii, shadows) | `frontend/src/styles/genwatch.css` (`:root` and `[data-theme="light"]`) |
| Icon library            | `frontend/src/components/primitives.tsx` — `Icon` |
| Shared primitives       | same file — `Pill`, `Sparkline`, `LineChart`, `Card`, `Modal`, `LiveTick`, `Switch` |
| Live view + hero + ATS  | `frontend/src/views/LiveView.tsx` |
| History view + chart    | `frontend/src/views/HistoryView.tsx` |
| Events + alarm log      | `frontend/src/views/EventsView.tsx` |
| Settings + register map | `frontend/src/views/SettingsView.tsx` |
| Confirm modal           | `frontend/src/views/ConfirmModal.tsx` |
| API contract            | `backend/genwatch/api/*` (mirrored in repo-root `README.md`) |
| WebSocket push          | `backend/genwatch/api/ws.py` + `frontend/src/hooks/useLiveData.ts` |

## Fonts

- [Geist](https://fonts.google.com/specimen/Geist) — sans, weights 300/400/500/600/700.
- [JetBrains Mono](https://fonts.google.com/specimen/JetBrains+Mono) — mono, weights 400/500/600.

Loaded from Google Fonts via the `<link>` tag in `frontend/index.html`.
