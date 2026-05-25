// API types — mirror the FastAPI responses in backend/genwatch/api/*.

export type EngineState =
  | "stopped"
  | "cranking"
  | "running"
  | "exercising"
  | "cooling"
  | "alarm"
  | "unknown";

export type CommsState = "healthy" | "degraded" | "lost";

export type Severity = "ok" | "info" | "warn" | "alarm";

export type Role = "viewer" | "operator" | "admin";

export interface Reading {
  rpm: number | null;
  hz: number | null;
  kw: number | null;
  oilP: number | null;
  coolT: number | null;
  batt: number | null;
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

export interface StatusBody {
  state: EngineState;
  alarmRaw: number;
  timeInState: number;
  stateStartedAt: number;
  comms: CommsHealth;
  reading: Reading;
  site: {
    id: string;
    name: string;
    ratingKw: number;
    engine: string;
    tankGal: number;
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
  panel: {
    mode: "auto" | "manual" | "off" | "unknown";
    keySwitchRaw: number | null;
    engineStatusCode: number | null;
    activeAlarmCountHw: number | null;
    quietTestStatusRaw: number | null;
  };
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
    }
  | { type: "transition"; from: EngineState; to: EngineState; ts: number }
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
}
