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

// Global hook fired when any non-login request comes back 401 — lets the
// app drop the operator back to the login screen on a mid-session expiry
// (or secret rotation) instead of leaving a frozen, live-looking
// dashboard. App registers this; null when unmounted.
let unauthorizedHandler: (() => void) | null = null;
export function setUnauthorizedHandler(fn: (() => void) | null) {
  unauthorizedHandler = fn;
}

async function request<T>(
  path: string,
  init: RequestInit = {}
): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      // Custom header that browsers will not allow a cross-origin form
      // POST to set without a CORS preflight — adds a second layer of
      // CSRF defense alongside the server's Origin/Referer middleware.
      // The server doesn't currently require it (would break legitimate
      // non-browser clients), but the FE marks every request so the
      // header is available if/when server-side enforcement lands.
      "X-Requested-With": "XMLHttpRequest",
      ...(init.headers || {}),
    },
    ...init,
  });
  const text = await res.text();
  let data: unknown = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      // Non-JSON body — e.g. an HTML 502/504 page from a reverse proxy.
      // Keep the raw text as the body so the caller still sees a typed
      // ApiError WITH the real status, not a raw SyntaxError.
      data = text;
    }
  }
  // A 401 on anything other than the login attempt itself means the
  // session lapsed — surface it globally so the UI returns to login.
  if (res.status === 401 && path !== "/api/auth/login") {
    unauthorizedHandler?.();
  }
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
  ackAlarm: async (code: string) => {
    // Server requires a fresh confirm_token on /api/alarms/{code}/ack
    // (same gate as start/stop/transfer). The token is single-use,
    // operator-bound, and 30s-TTL — issued via /api/control/confirm
    // and consumed by the ack POST. Cross-site attackers can't read
    // the confirm response body (same-origin policy), so chaining
    // confirm→ack also closes the CSRF hole on this endpoint that
    // existed when only an authenticated session was required.
    const tok = await request<ConfirmToken>("/api/control/confirm?verb=ack");
    return request<{ ok: boolean; code: string; hw_ack: boolean }>(
      `/api/alarms/${encodeURIComponent(code)}/ack`,
      {
        method: "POST",
        body: JSON.stringify({ confirm_token: tok.token }),
      }
    );
  },
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

  // Control flow. `verb` binds the token to the action it will confirm
  // (start/stop/exercise/transfer, an ATS command, or "ack") so a token
  // can't be cross-spent between two open confirm dialogs.
  confirmToken: (verb?: string) =>
    request<ConfirmToken>(
      "/api/control/confirm" + (verb ? `?verb=${encodeURIComponent(verb)}` : "")
    ),
  control: (verb: "start" | "stop" | "exercise" | "transfer", confirm_token: string) =>
    request<{ ok: boolean }>(`/api/control/${verb}`, {
      method: "POST",
      body: JSON.stringify({ confirm_token }),
    }),

  // ATS-Pi commands (Phase 3). All confirm-token gated via the shared
  // /api/control/confirm token. `assert` selects assert vs release for
  // the maintained commands (inhibit / force-transfer); `override` is
  // required by the backend to force-transfer while utility is available.
  atsCommand: (
    cmd: "test" | "inhibit" | "force_transfer" | "bypass_delay",
    confirm_token: string,
    opts: { assert?: boolean; override?: boolean } = {}
  ) => {
    const path =
      cmd === "force_transfer" ? "/api/ats/force-transfer"
      : cmd === "bypass_delay" ? "/api/ats/bypass-delay"
      : `/api/ats/${cmd}`;
    const body: Record<string, unknown> = { confirm_token };
    if (cmd === "inhibit" || cmd === "force_transfer") body.assert = opts.assert ?? true;
    if (cmd === "force_transfer") body.override = opts.override ?? false;
    return request<{ ok: boolean; command: string; register: string }>(path, {
      method: "POST",
      body: JSON.stringify(body),
    });
  },

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
};

export const EMPTY_READING: Reading = {
  rpm: null, hz: null, kw: null, pf: null,
  oilP: null, oilT: null, coolT: null, coolLevel: null,
  throttle: null, o2: null, batt: null, battA: null,
  vAB: null, vBC: null, vCA: null, iA: null, iB: null, iC: null,
  fuelPct: null, runHours: null, startCount: null,
};
