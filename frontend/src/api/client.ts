// Thin fetch wrapper. Keeps auth, error shape and JSON parsing in one place.

import type {
  ConfirmToken,
  EventRow,
  FuelStatus,
  MaintenanceDue,
  MaintenanceLogEntry,
  MaintenanceSchedule,
  MeBody,
  Outage,
  OutageSummary,
  Reading,
  SlackUpdate,
  StatusBody,
  SystemHealth,
} from "../types";

const BASE = ""; // same-origin in production; Vite proxy in dev

export class ApiError extends Error {
  status: number;
  body: unknown;
  constructor(status: number, body: unknown, message?: string) {
    super(message ?? `${status}`);
    this.status = status;
    this.body = body;
  }
}

async function request<T>(
  path: string,
  init: RequestInit = {}
): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      ...(init.headers || {}),
    },
    ...init,
  });
  const text = await res.text();
  const data = text ? (JSON.parse(text) as unknown) : null;
  if (!res.ok) throw new ApiError(res.status, data);
  return data as T;
}

export const api = {
  me: () => request<MeBody>("/api/auth/me"),
  login: (password: string) =>
    request<{ ok: boolean; operator: string; role: string }>(
      "/api/auth/login",
      { method: "POST", body: JSON.stringify({ password }) }
    ),
  logout: () => request<{ ok: true }>("/api/auth/logout", { method: "POST" }),
  status: () => request<StatusBody>("/api/status"),
  health: () => request<{ ok: boolean; mock: boolean; version: string; commsState: string }>("/api/health"),
  events: (params: {
    limit?: number;
    severity?: string[];
    type?: string;
    from?: number;
    to?: number;
  } = {}) => {
    const q = new URLSearchParams();
    if (params.limit) q.set("limit", String(params.limit));
    if (params.severity?.length) q.set("severity", params.severity.join(","));
    if (params.type) q.set("type", params.type);
    if (params.from) q.set("from", String(params.from));
    if (params.to) q.set("to", String(params.to));
    return request<{ count: number; events: EventRow[] }>(
      `/api/events${q.toString() ? `?${q}` : ""}`
    );
  },
  alarms: () =>
    request<{ alarms: Array<{ code: string; desc: string; severity: string; raised_at: number; raw: number }> }>(
      "/api/alarms?active=true"
    ),
  ackAlarm: (code: string) =>
    request<{ ok: boolean }>(`/api/alarms/${encodeURIComponent(code)}/ack`, { method: "POST" }),
  alarmCodes: () =>
    request<{ codes: Array<{ code: string; desc: string; severity: string }> }>("/api/alarm-codes"),

  telemetry: (params: { metric: string; from?: number; to?: number; maxPoints?: number }) => {
    const q = new URLSearchParams({ metric: params.metric });
    if (params.from) q.set("from", String(params.from));
    if (params.to) q.set("to", String(params.to));
    if (params.maxPoints) q.set("max_points", String(params.maxPoints));
    return request<{
      metric: string;
      column: string;
      from: number;
      to: number;
      count: number;
      points: [number, number][];
    }>(`/api/telemetry?${q}`);
  },

  // Control flow
  confirmToken: () => request<ConfirmToken>("/api/control/confirm"),
  control: (verb: "start" | "stop" | "exercise" | "transfer", confirm_token: string) =>
    request<{ ok: boolean }>(`/api/control/${verb}`, {
      method: "POST",
      body: JSON.stringify({ confirm_token }),
    }),

  // Settings
  config: () => request<any>("/api/config"),
  updateConfig: (body: {
    transport?: "serial" | "tcp";
    serial?: any;
    modbus_tcp?: any;
    modbus?: any;
    retention?: any;
    slack?: SlackUpdate;
    ws_push_ms?: number;
  }) =>
    request<{ ok: boolean; restart_required: boolean; slack_updated?: boolean }>(
      "/api/config",
      {
        method: "PUT",
        body: JSON.stringify(body),
      }
    ),
  testSlack: () =>
    request<{ ok: boolean; detail: string }>("/api/slack/test", { method: "POST" }),
  registers: () =>
    request<{
      path: string;
      slave: number;
      primePollMs: number;
      basePollMs: number;
      registers: Array<{
        addr: string;
        name: string;
        fc: string;
        type: string;
        tier: string;
        group: string;
        unit: string;
        scale: number | null;
        value: number | null;
      }>;
    }>("/api/registers"),
  reloadRegisters: () =>
    request<{ ok: boolean; registers: number; controls: number }>("/api/registers/reload", { method: "POST" }),
  verifyRegisters: () =>
    request<{
      ok: boolean;
      static: { ok: boolean; errors: string[]; warnings: string[] };
      live: {
        skipped: boolean;
        ok: boolean;
        tested: number;
        failed: number;
        failures: Array<{ name: string; addr: string; fc: number; error: string | null }>;
      };
    }>("/api/registers/verify"),

  // ── Post-audit additions ──────────────────────────────────────────────

  system: () => request<SystemHealth>("/api/system"),

  fuel: () => request<FuelStatus>("/api/fuel"),

  outages: (params: { limit?: number; days?: number } = {}) => {
    const q = new URLSearchParams();
    if (params.limit) q.set("limit", String(params.limit));
    if (params.days) q.set("days", String(params.days));
    return request<{
      outages: Outage[];
      summary30d: OutageSummary;
      summary365d: OutageSummary;
    }>(`/api/outages${q.toString() ? `?${q}` : ""}`);
  },
  annotateOutage: (id: number, notes: string) =>
    request<{ ok: boolean }>(`/api/outages/${id}/notes`, {
      method: "POST", body: JSON.stringify({ notes }),
    }),

  maintenance: {
    schedule: () =>
      request<{ schedule: MaintenanceSchedule[] }>("/api/maintenance/schedule"),
    upsertSchedule: (body: {
      kind: string;
      interval_hours: number;
      interval_days: number;
      notes?: string;
      enabled?: boolean;
    }) =>
      request<{ ok: boolean; id: number }>("/api/maintenance/schedule", {
        method: "PUT", body: JSON.stringify(body),
      }),
    deleteSchedule: (kind: string) =>
      request<{ ok: boolean }>(`/api/maintenance/schedule/${encodeURIComponent(kind)}`, {
        method: "DELETE",
      }),
    log: (kind?: string, limit = 500) => {
      const q = new URLSearchParams({ limit: String(limit) });
      if (kind) q.set("kind", kind);
      return request<{ log: MaintenanceLogEntry[] }>(`/api/maintenance/log?${q}`);
    },
    addLog: (body: {
      kind: string;
      run_hours_at?: number | null;
      performed_by?: string;
      notes?: string;
      cost_usd?: number | null;
      ts?: number | null;
    }) =>
      request<{ ok: boolean; id: number; runHoursAt: number | null }>(
        "/api/maintenance/log",
        { method: "POST", body: JSON.stringify(body) },
      ),
    due: () =>
      request<{ currentRunHours: number | null; items: MaintenanceDue[] }>(
        "/api/maintenance/due",
      ),
  },

  // Export URLs are returned as href strings — the browser handles the
  // download (Content-Disposition: attachment). Cookie auth flows through.
  exportTelemetryUrl: (params: { from?: number; to?: number; columns?: string } = {}) => {
    const q = new URLSearchParams();
    if (params.from) q.set("from", String(params.from));
    if (params.to) q.set("to", String(params.to));
    if (params.columns) q.set("columns", params.columns);
    return `/api/telemetry/export${q.toString() ? `?${q}` : ""}`;
  },
  exportEventsUrl: (params: {
    from?: number; to?: number; severity?: string; type?: string;
  } = {}) => {
    const q = new URLSearchParams();
    if (params.from) q.set("from", String(params.from));
    if (params.to) q.set("to", String(params.to));
    if (params.severity) q.set("severity", params.severity);
    if (params.type) q.set("type", params.type);
    return `/api/events/export${q.toString() ? `?${q}` : ""}`;
  },
  exportAuditUrl: (params: { from?: number; to?: number } = {}) => {
    const q = new URLSearchParams();
    if (params.from) q.set("from", String(params.from));
    if (params.to) q.set("to", String(params.to));
    return `/api/audit/export${q.toString() ? `?${q}` : ""}`;
  },

  backupDownloadUrl: () => "/api/backup/download",
  restoreBackup: async (file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    const res = await fetch("/api/backup/restore", {
      method: "POST",
      credentials: "include",
      body: fd,
    });
    const text = await res.text();
    const data = text ? JSON.parse(text) : null;
    if (!res.ok) throw new ApiError(res.status, data);
    return data as { ok: boolean; restored: string[]; restart_required: boolean; note: string };
  },
};

export const EMPTY_READING: Reading = {
  rpm: null, hz: null, kw: null, pf: null,
  oilP: null, oilT: null, coolT: null, coolLevel: null,
  throttle: null, o2: null, batt: null, battA: null,
  vAB: null, vBC: null, vCA: null, iA: null, iB: null, iC: null,
  fuelPct: null, runHours: null, startCount: null,
};
