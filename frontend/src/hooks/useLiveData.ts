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
}

export function useLiveData(): LiveState {
  const [state, setState] = useState<LiveState>({
    loading: true,
    error: null,
    status: null,
    history: [],
    reconnects: 0,
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
        // Reconnect with exponential backoff (max 30s).
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

        switch (msg.type) {
          case "snapshot": {
            s = {
              ...s,
              state: msg.state,
              alarmRaw: msg.alarmRaw,
              timeInState: msg.timeInState,
              comms: msg.comms,
              reading: mergeReading(s.reading, msg.reading),
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
        return { ...cur, status: s, history };
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
