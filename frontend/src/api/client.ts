// Thin fetch wrapper. Keeps auth, error shape and JSON parsing in one place.

import type {
  ConfirmToken,
  EventRow,
  MeBody,
  Reading,
  SlackUpdate,
  StatusBody,
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
    serial?: any;
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
};

export const EMPTY_READING: Reading = {
  rpm: null, hz: null, kw: null, oilP: null, coolT: null, batt: null,
  vAB: null, vBC: null, vCA: null, iA: null, iB: null, iC: null,
  fuelPct: null, runHours: null, startCount: null,
};
