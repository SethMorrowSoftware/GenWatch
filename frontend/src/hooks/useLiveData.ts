// Single source of truth for the live state:
//   - GET /api/status on mount → seed everything
//   - WebSocket /ws/live → push updates
//
// Returns:
//   loading, error, status, history[]
//
// History is a rolling buffer of last N readings, suitable for the
// sparklines on the Live view.

import { useEffect, useRef, useState } from "react";
import { api, EMPTY_READING } from "../api/client";
import type {
  ActiveAlarm,
  AtsBlock,
  CommsHealth,
  EngineState,
  LiveMessage,
  Reading,
  StatusBody,
} from "../types";

const HISTORY_SIZE = 40;

export interface LiveState {
  loading: boolean;
  error: string | null;
  status: StatusBody | null;
  history: Reading[];
  reconnects: number;
  // Wall-clock instant (Date.now()) when we last received a snapshot or
  // transition push from the server, or null if we've never received
  // one. Consumers compute "stale" against this — never against
  // status.serverTs, which is the controller's wall clock and can drift.
  lastPushAt: number | null;
  // Wall-clock instant when we last received a snapshot that included
  // a `panel` block, or null if we've never received one. Tracked
  // separately from lastPushAt so a backend that emits snapshots
  // without `panel` (older version, or a future code change that
  // accidentally drops the field) doesn't let the control-button
  // panel-mode gate run against a frozen seed value. ControlsPanel
  // gates Start/Stop/Transfer on this AND lastPushAt being recent.
  panelLastSeenAt: number | null;
  // True when the WebSocket is currently closed and waiting to
  // reconnect. The UI uses this together with lastPushAt to decide
  // whether to show a "STALE DATA" badge.
  wsDown: boolean;
}

export function useLiveData(): LiveState {
  const [state, setState] = useState<LiveState>({
    loading: true,
    error: null,
    status: null,
    history: [],
    reconnects: 0,
    lastPushAt: null,
    panelLastSeenAt: null,
    wsDown: false,
  });
  const historyRef = useRef<Reading[]>([]);

  useEffect(() => {
    let cancelled = false;
    let ws: WebSocket | null = null;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
    let stableTimer: ReturnType<typeof setTimeout> | null = null;
    let backoff = 1000;

    const clearTimers = () => {
      if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
      if (stableTimer) { clearTimeout(stableTimer); stableTimer = null; }
    };
    const detach = (sock: WebSocket | null) => {
      if (sock) sock.onopen = sock.onmessage = sock.onclose = sock.onerror = null;
    };

    const seed = async () => {
      try {
        const s = await api.status();
        if (cancelled) return;
        historyRef.current = [s.reading].concat(historyRef.current).slice(0, HISTORY_SIZE);
        // The REST seed always includes a panel block (server-side
        // mirror of the WS shape), so it counts as a fresh panel
        // observation — but only until staleThresholdMs passes
        // without a WS confirmation, at which point ControlsPanel
        // will block remote commands until live data resumes.
        setState((cur) => ({
          ...cur,
          loading: false,
          error: null,
          status: s,
          history: historyRef.current,
          lastPushAt: Date.now(),
          panelLastSeenAt: Date.now(),
        }));
      } catch (e: any) {
        if (cancelled) return;
        setState((cur) => ({ ...cur, loading: false, error: e?.message ?? "fetch failed" }));
      }
    };

    const openWs = () => {
      // Tear down any previous socket + pending timers first so stale
      // handlers can't fire setState or schedule a *second* reconnect
      // (which would multiply sockets on every cycle).
      clearTimers();
      detach(ws);
      try { ws?.close(); } catch { /* already closed */ }

      const proto = window.location.protocol === "https:" ? "wss" : "ws";
      const url = `${proto}://${window.location.host}/ws/live`;
      const sock = new WebSocket(url);
      ws = sock;

      sock.onopen = () => {
        if (cancelled || ws !== sock) return;
        setState((cur) => ({ ...cur, wsDown: false }));
        // Only reset the backoff after the connection has stayed up a
        // few seconds. Resetting it on open (the old behaviour) let a
        // half-broken server that accepts then immediately drops every
        // socket get hammered in a tight 1 s loop forever.
        stableTimer = setTimeout(() => { backoff = 1000; }, 3000);
      };
      sock.onmessage = (ev) => {
        if (cancelled || ws !== sock) return;
        let msg: LiveMessage;
        try {
          msg = JSON.parse(ev.data);
        } catch (e) {
          console.warn("ws: failed to parse message", e);
          return;
        }
        applyMessage(msg);
      };
      sock.onclose = () => {
        if (cancelled || ws !== sock) return;
        if (stableTimer) { clearTimeout(stableTimer); stableTimer = null; }
        // Mark wsDown so the UI surfaces a stale-data warning instead of
        // pretending the last reading is still current.
        setState((cur) => ({ ...cur, wsDown: true }));
        // Backoff with jitter (max 30 s). Grow AFTER capturing this
        // attempt's wait so successive closes back off 1s, 1.8s, 3.2s…
        const wait = Math.min(backoff, 30000) + Math.random() * 250;
        backoff = Math.min(backoff * 1.8, 30000);
        reconnectTimer = setTimeout(() => {
          if (cancelled) return;
          setState((cur) => ({ ...cur, reconnects: cur.reconnects + 1 }));
          openWs();
        }, wait);
      };
      sock.onerror = () => { try { sock.close(); } catch { /* noop */ } };
    };

    const applyMessage = (msg: LiveMessage) => {
      setState((cur) => {
        if (!cur.status) return cur;
        let s: StatusBody = cur.status;
        let history = historyRef.current;
        // Freshness clock: only a `snapshot` or `transition` proves the
        // H-100 poll loop actually produced fresh data this tick. Keepalive
        // `ping`s — and `hello`/`event`/`ats-*` diagnostics — must NOT
        // reset it; otherwise a frozen poll loop whose WebSocket keeps
        // pinging would hold off the STALE badge and keep control buttons
        // enabled against stale numbers. Bumped to the client wall clock
        // (drift-immune vs the controller's clock) only in those cases.
        let lastPushAt = cur.lastPushAt;
        // Panel freshness is a SEPARATE clock: only refreshed when a
        // snapshot actually carries the `panel` block. Without this,
        // a backend that drops `panel` from its snapshots (older
        // version, or a future bug) would let the UI's panel-mode
        // gate run forever against the boot seed — an operator key
        // turn at the unit would never propagate. Default to carrying
        // forward; only the snapshot case below bumps it.
        let panelLastSeenAt = cur.panelLastSeenAt;

        switch (msg.type) {
          case "snapshot": {
            lastPushAt = Date.now();
            if (msg.panel) {
              panelLastSeenAt = Date.now();
            }
            s = {
              ...s,
              state: msg.state,
              alarmRaw: msg.alarmRaw,
              timeInState: msg.timeInState,
              comms: msg.comms,
              reading: mergeReading(s.reading, msg.reading),
              // Carry the panel block forward when the backend sends it
              // (v0.1.1+). Without this, an on-site operator toggling
              // AUTO ↔ MANUAL would leave the remote UI's panel chip
              // and control-button enable state stuck on the seed value
              // from /api/status. Fall back to the previous block if
              // the message predates the field so older backends still
              // work.
              panel: msg.panel ?? s.panel,
              // Load source — preserve previous if backend predates
              // the field. When present we also derive the legacy
              // hts.transferredToGen flag here so the ATS card stays
              // in sync without a redundant round-trip.
              loadSource: msg.loadSource ?? s.loadSource,
              timeInLoadSource: msg.timeInLoadSource ?? s.timeInLoadSource,
              hts:
                msg.loadSource !== undefined
                  ? { ...s.hts, transferredToGen: msg.loadSource === "generator" }
                  : s.hts,
              // ATS block — backend sends null when ats.enabled=false,
              // omits the field entirely on older deployments. Preserve
              // previous when not provided so the UI doesn't blank out
              // on a transient null.
              ats: msg.ats ? mergeAts(s.ats, msg.ats) : s.ats,
              serverTs: msg.ts,
            };
            history = [s.reading, ...history].slice(0, HISTORY_SIZE);
            historyRef.current = history;
            break;
          }
          case "transition": {
            lastPushAt = Date.now();
            s = { ...s, state: msg.to, stateStartedAt: msg.ts, timeInState: 0 };
            break;
          }
          case "load-source": {
            // Immediate UI response to a utility↔generator transition,
            // independent of the next snapshot tick. Also keep the
            // legacy hts.transferredToGen flag in lockstep so the ATS
            // diagram flips instantly.
            s = {
              ...s,
              loadSource: msg.to,
              loadSourceStartedAt: msg.ts,
              timeInLoadSource: 0,
              hts: { ...s.hts, transferredToGen: msg.to === "generator" },
            };
            break;
          }
          case "ats-position": {
            // When the ATS-Pi is authoritative, this event is the
            // canonical signal for a position change — the backend
            // suppresses the redundant `load-source` event for the
            // same transition (see services/state.py). So we update
            // BOTH ats.position AND loadSource here, keeping the
            // ATS card diagram and the hero badge in sync within a
            // single tick instead of waiting for the next H-100 poll.
            if (s.ats.enabled) {
              const updateLoadSource = s.ats.authoritative;
              s = {
                ...s,
                ats: { ...s.ats, position: msg.to },
                loadSource: updateLoadSource ? msg.to : s.loadSource,
                loadSourceStartedAt: updateLoadSource ? msg.ts : s.loadSourceStartedAt,
                timeInLoadSource: updateLoadSource ? 0 : s.timeInLoadSource,
                hts: updateLoadSource
                  ? { ...s.hts, transferredToGen: msg.to === "generator" }
                  : s.hts,
              };
            }
            break;
          }
          case "ats-source": {
            // Source-availability transition (utility lost/restored,
            // gen ready/unavailable).
            if (s.ats.enabled) {
              const patch =
                msg.source === "normal"
                  ? { normalAvailable: msg.available }
                  : { emergencyAvailable: msg.available };
              s = { ...s, ats: { ...s.ats, ...patch } };
            }
            break;
          }
          case "ats-mode": {
            if (s.ats.enabled) {
              s = { ...s, ats: { ...s.ats, atsMode: msg.to } };
            }
            break;
          }
          case "ats-comms": {
            // The ATS link's own comms health, separate from the
            // H-100's. When the ATS link goes down, the loadSource
            // automatically reverts to H-100 derivation via the
            // backend's precedence rule (next snapshot will reflect
            // that); we just update the displayed comms badge here.
            if (s.ats.enabled) {
              s = {
                ...s,
                ats: {
                  ...s.ats,
                  comms: { state: msg.to, successPct: msg.successPct },
                  // If comms went bad, authoritative flips to false
                  // immediately (matches backend is_authoritative()).
                  authoritative: msg.to === "healthy" && s.ats.authoritative,
                },
              };
            }
            break;
          }
          case "ats-reboot": {
            // Diagnostic event — no UI state change needed (the next
            // snapshot will refresh uptime). The events feed will
            // show the reboot row.
            break;
          }
          case "alarm": {
            // Append to activeAlarms if not present
            const exists = s.activeAlarms.find((a: ActiveAlarm) => a.code === msg.code);
            if (!exists) {
              const a: ActiveAlarm = {
                code: msg.code,
                desc: msg.desc,
                severity: msg.severity,
                raised_at: msg.ts,
                raw: 0,
              };
              s = { ...s, activeAlarms: [a, ...s.activeAlarms] };
            }
            break;
          }
          case "alarm-cleared": {
            s = { ...s, activeAlarms: s.activeAlarms.filter((a) => a.code !== msg.code) };
            break;
          }
          case "hello":
          case "ping":
          case "event":
            // No state change for these here; events view re-fetches.
            break;
        }
        return { ...cur, status: s, history, lastPushAt, panelLastSeenAt };
      });
    };

    seed().then(() => {
      if (!cancelled) openWs();
    });

    return () => {
      cancelled = true;
      clearTimers();
      detach(ws);
      try { ws?.close(); } catch { /* already closed */ }
    };
  }, []);

  return state;
}

function mergeReading(prev: Reading, patch: Partial<Reading>): Reading {
  const out: Reading = { ...EMPTY_READING, ...prev };
  for (const k in patch) {
    // Assign even when the value is null. A snapshot reports a field as
    // null when the sensor dropped out or the poller evicted a stale
    // last-good value (per-register TTL) — we must clear the displayed
    // number rather than keep showing a frozen reading forever. Keys
    // ABSENT from the patch keep their previous value.
    (out as any)[k] = (patch as any)[k] ?? null;
  }
  return out;
}

/** Merge an incoming ATS snapshot into the previous block.
 *
 * Identification fields (icdVersion / fw / unitId / uptimeS) typically
 * arrive only on the REST seed; WS snapshots omit them to keep the
 * payload small. We preserve them across pushes here so the UI's
 * "ICD v1.0" badge etc. stay rendered.
 */
function mergeAts(prev: AtsBlock, incoming: AtsBlock): AtsBlock {
  if (!incoming.enabled) return incoming;
  if (!prev.enabled) return incoming;
  return {
    ...prev,
    ...incoming,
    icdVersion: incoming.icdVersion ?? prev.icdVersion,
    atsPiFw: incoming.atsPiFw ?? prev.atsPiFw,
    atsPiUnitId: incoming.atsPiUnitId ?? prev.atsPiUnitId,
    atsPiUptimeS: incoming.atsPiUptimeS ?? prev.atsPiUptimeS,
  };
}
