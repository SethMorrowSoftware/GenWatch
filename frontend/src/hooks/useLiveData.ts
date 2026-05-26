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
    wsDown: false,
  });
  const historyRef = useRef<Reading[]>([]);

  useEffect(() => {
    let cancelled = false;
    let ws: WebSocket | null = null;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
    let backoff = 1000;

    const seed = async () => {
      try {
        const s = await api.status();
        if (cancelled) return;
        historyRef.current = [s.reading].concat(historyRef.current).slice(0, HISTORY_SIZE);
        setState((cur) => ({
          ...cur,
          loading: false,
          error: null,
          status: s,
          history: historyRef.current,
          lastPushAt: Date.now(),
        }));
      } catch (e: any) {
        if (cancelled) return;
        setState((cur) => ({ ...cur, loading: false, error: e?.message ?? "fetch failed" }));
      }
    };

    const openWs = () => {
      const proto = window.location.protocol === "https:" ? "wss" : "ws";
      const url = `${proto}://${window.location.host}/ws/live`;
      ws = new WebSocket(url);

      ws.onopen = () => {
        backoff = 1000;
        setState((cur) => ({ ...cur, wsDown: false }));
      };
      ws.onmessage = (ev) => {
        if (cancelled) return;
        let msg: LiveMessage;
        try {
          msg = JSON.parse(ev.data);
        } catch (e) {
          console.warn("ws: failed to parse message", e);
          return;
        }
        applyMessage(msg);
      };
      ws.onclose = () => {
        if (cancelled) return;
        // Reconnect with exponential backoff (max 30s). Mark wsDown so
        // the UI can surface a stale-data warning instead of pretending
        // the last reading we have is still current.
        setState((cur) => ({ ...cur, wsDown: true }));
        reconnectTimer = setTimeout(() => {
          backoff = Math.min(backoff * 1.8, 30000);
          setState((cur) => ({ ...cur, reconnects: cur.reconnects + 1 }));
          openWs();
        }, backoff);
      };
      ws.onerror = () => ws?.close();
    };

    const applyMessage = (msg: LiveMessage) => {
      setState((cur) => {
        if (!cur.status) return cur;
        let s: StatusBody = cur.status;
        let history = historyRef.current;
        // Any inbound message proves the WS is delivering — keep the
        // freshness clock on `Date.now()` (client wall clock) so the
        // stale check can't be fooled by a controller clock that drifts.
        const lastPushAt = Date.now();

        switch (msg.type) {
          case "snapshot": {
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
              serverTs: msg.ts,
            };
            history = [s.reading, ...history].slice(0, HISTORY_SIZE);
            historyRef.current = history;
            break;
          }
          case "transition": {
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
        return { ...cur, status: s, history, lastPushAt };
      });
    };

    seed().then(() => {
      if (!cancelled) openWs();
    });

    return () => {
      cancelled = true;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      ws?.close();
    };
  }, []);

  return state;
}

function mergeReading(prev: Reading, patch: Partial<Reading>): Reading {
  const out: Reading = { ...EMPTY_READING, ...prev };
  for (const k in patch) {
    const v = (patch as any)[k];
    if (v !== null && v !== undefined) (out as any)[k] = v;
  }
  return out;
}
