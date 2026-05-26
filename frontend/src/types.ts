// API types — mirror the FastAPI responses in backend/genwatch/api/*.

export type EngineState =
  | "stopped"
  | "cranking"
  | "running"
  | "exercising"
  | "cooling"
  | "alarm"
  | "unknown";

// Which source is currently supplying the load. Inferred from engine
// state + output kW/current — see backend services/state.py. Until the
// first base-tier poll completes after boot, this defaults to 'utility'.
export type LoadSource = "utility" | "generator" | "unknown";

export type CommsState = "healthy" | "degraded" | "lost";

export type Severity = "ok" | "info" | "warn" | "alarm";

export type Role = "viewer" | "operator" | "admin";

export interface Reading {
  rpm: number | null;
  hz: number | null;
  kw: number | null;
  pf: number | null;
  oilP: number | null;
  oilT: number | null;
  coolT: number | null;
  coolLevel: number | null;
  throttle: number | null;
  o2: number | null;
  batt: number | null;
  battA: number | null;
  vAB: number | null;
  vBC: number | null;
  vCA: number | null;
  iA: number | null;
  iB: number | null;
  iC: number | null;
  fuelPct: number | null;
  runHours: number | null;
  startCount: number | null;
}

export interface CommsHealth {
  state: CommsState;
  successPct: number;
  lastGoodAt: number | null;
  rateMs: number;
  p95LatencyMs: number;
}

export interface ActiveAlarm {
  code: string;
  desc: string;
  severity: Severity;
  raised_at: number;
  raw: number;
}

export interface PanelBlock {
  mode: "auto" | "manual" | "off" | "unknown";
  keySwitchRaw: number | null;
  engineStatusCode: number | null;
  activeAlarmCountHw: number | null;
  quietTestStatusRaw: number | null;
}

export interface StatusBody {
  state: EngineState;
  alarmRaw: number;
  timeInState: number;
  stateStartedAt: number;
  // Derived: 'utility' | 'generator' | 'unknown'. See LoadSource.
  loadSource: LoadSource;
  loadSourceStartedAt: number;
  timeInLoadSource: number;
  comms: CommsHealth;
  reading: Reading;
  site: {
    id: string;
    name: string;
    ratingKw: number;
    engine: string;
    tankGal: number;
    // 'diesel' | 'gaseous' | 'unknown' — drives UI gating (hide O₂ on
    // diesel, etc.). Optional for forward-compat with older backends.
    fuelType?: "diesel" | "gaseous" | "unknown";
  };
  exercise: {
    enabled: boolean;
    day: string;
    time: string;
    durationMin: number;
  };
  activeAlarms: ActiveAlarm[];
  hts: {
    transferredToGen: boolean;
    lastTransferTs: number | null;
    transfers30d: number;
  };
  lastAlarm: {
    ts: number;
    severity: Severity;
    message: string;
  } | null;
  panel: PanelBlock;
  serverTs: number;
}

export interface EventRow {
  id: number;
  ts: number;
  severity: Severity;
  type: string;
  message: string;
  meta: string | null;
}

export interface ConfirmToken {
  token: string;
  issuedAt: number;
  expiresAt: number;
}

export type LiveMessage =
  | { type: "hello"; state: EngineState; comms: Partial<CommsHealth>; serverTs: number }
  | { type: "ping" }
  | {
      type: "snapshot";
      ts: number;
      state: EngineState;
      timeInState: number;
      alarmRaw: number;
      comms: CommsHealth;
      reading: Reading;
      // Optional for forward-compat with older backends — present from
      // v0.1.1 onwards. Used to gate the control buttons on the
      // H-100 front-panel key switch being in AUTO.
      panel?: PanelBlock;
      // Optional for forward-compat — present once the load-source
      // derivation lands server-side. The hook falls back to the
      // seeded value when the field is absent.
      loadSource?: LoadSource;
      timeInLoadSource?: number;
    }
  | { type: "transition"; from: EngineState; to: EngineState; ts: number }
  | { type: "load-source"; from: LoadSource; to: LoadSource; ts: number }
  | { type: "alarm"; code: string; desc: string; severity: Severity; ts: number }
  | { type: "alarm-cleared"; code: string; ts: number }
  | { type: "event"; sev: Severity; eventType: string; msg: string; meta: string; ts: number };

export interface MeBody {
  authenticated: boolean;
  operator?: string;
  role?: Role;
}

// Returned by GET /api/config.slack — the bot token itself is never
// exposed; only a flag confirming it is set on disk.
export interface SlackConfigView {
  enabled: boolean;
  channel: string;
  siteLabel: string;
  botTokenConfigured: boolean;
  alertOnAlarm: boolean;
  alertOnWarning: boolean;
  alertOnAlarmCleared: boolean;
  alertOnStateChange: boolean;
  alertOnCommand: boolean;
  alertOnCommsLost: boolean;
  alertOnLoadSourceChange: boolean;
}

// Sent in PUT /api/config.slack — omit a field to leave it unchanged.
// Set bot_token to "" to explicitly clear it.
export interface SlackUpdate {
  enabled?: boolean;
  bot_token?: string;
  channel?: string;
  site_label?: string;
  alert_on_alarm?: boolean;
  alert_on_warning?: boolean;
  alert_on_alarm_cleared?: boolean;
  alert_on_state_change?: boolean;
  alert_on_command?: boolean;
  alert_on_comms_lost?: boolean;
  alert_on_load_source_change?: boolean;
}
