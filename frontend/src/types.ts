// API types — mirror the FastAPI responses in backend/genwatch/api/*.

export type EngineState =
  | "stopped"
  | "cranking"
  | "running"
  | "exercising"
  | "cooling"
  | "alarm"
  | "unknown";

// Which source is currently supplying the load. Determined by either
//   1. The ATS-Pi companion device's direct position contacts, when
//      configured and healthy (docs/integrations/ats-pi-icd.md §10), or
//   2. GenWatch's H-100-electrical inference (services/state.py) as
//      the fallback when the ATS-Pi is absent or unreachable.
// 'transferring' is only produced by the ATS-Pi path — it represents
// the sub-second window during a load transfer when the switch
// contacts have opened from one source but not yet closed on the other.
export type LoadSource = "utility" | "generator" | "transferring" | "unknown";

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

// ATS-Pi companion device snapshot — present on every /api/status
// response (as either {enabled: false} or the full block). See
// docs/integrations/ats-pi-icd.md for field semantics.
export type AtsPosition = "utility" | "generator" | "transferring" | "unknown";
export type AtsMode = "auto" | "manual" | "test" | "unknown";

export interface AtsCommsHealth {
  state: CommsState;
  successPct: number;
}

// Discriminated union: when disabled, only `enabled` is present.
// When enabled, the full snapshot is delivered. The hook handles
// both shapes safely.
export type AtsBlock =
  | { enabled: false }
  | {
      enabled: true;
      position: AtsPosition;
      normalAvailable: boolean | null;
      emergencyAvailable: boolean | null;
      engineStartCalling: boolean | null;
      atsMode: AtsMode;
      faultCodes: string[];
      lastTransferToGenTs: number | null;
      lastRetransferToUtilTs: number | null;
      transferCount24h: number;
      transferCountLifetime: number;
      // Identification — present only on REST seed response (omitted
      // from WS pushes to keep frequent payloads small). Hook merges
      // them in when present.
      icdVersion?: [number, number];
      atsPiFw?: [number, number, number];
      atsPiUnitId?: number;
      atsPiUptimeS?: number;
      cmdTestActive: boolean;
      cmdInhibitActive: boolean;
      cmdForceTransferActive: boolean;
      cmdBypassDelayActive: boolean;
      comms: AtsCommsHealth;
      // True iff the ATS-Pi's position is currently driving the
      // operator-visible loadSource (vs the H-100 fallback derivation).
      // See ICD §10.
      authoritative: boolean;
    };

export interface StatusBody {
  state: EngineState;
  alarmRaw: number;
  timeInState: number;
  stateStartedAt: number;
  // Derived: 'utility' | 'generator' | 'transferring' | 'unknown'.
  // Driven by ATS-Pi when authoritative, falls back to H-100 telemetry.
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
  // ATS-Pi companion. Always present on /api/status responses (the
  // backend emits at least {enabled: false}); typed as required.
  ats: AtsBlock;
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
      // ATS-Pi block — null when ats.enabled=false on the backend,
      // populated otherwise. Same shape as REST /api/status.
      ats?: AtsBlock | null;
    }
  | { type: "transition"; from: EngineState; to: EngineState; ts: number }
  | { type: "load-source"; from: LoadSource; to: LoadSource; ts: number }
  | { type: "alarm"; code: string; desc: string; severity: Severity; ts: number }
  | { type: "alarm-cleared"; code: string; ts: number }
  // ATS-Pi events emitted by services/ats.py — drive immediate UI
  // updates without waiting for the next snapshot push.
  | { type: "ats-position"; from: AtsPosition; to: AtsPosition; ts: number }
  | { type: "ats-source"; source: "normal" | "emergency"; available: boolean; code: string; ts: number }
  | { type: "ats-mode"; from: AtsMode; to: AtsMode; ts: number }
  | { type: "ats-comms"; from: CommsState; to: CommsState; successPct: number; ts: number }
  | { type: "ats-reboot"; prev_uptime_s: number; new_uptime_s: number; ts: number }
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
