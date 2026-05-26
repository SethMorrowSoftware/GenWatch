// Events + alarms view: filter chips, event log, alarm codes reference.

import { useEffect, useState } from "react";
import { api } from "../api/client";
import { Card, EmptyState, Icon, Pill, Skeleton } from "../components/primitives";
import type { EventRow, Severity } from "../types";
import { relTime } from "./LiveView";

const ALL_SEVS: Severity[] = ["alarm", "warn", "info", "ok"];

interface AlarmCode {
  code: string;
  desc: string;
  severity: string;
}

export function EventsView() {
  const [events, setEvents] = useState<EventRow[]>([]);
  const [codes, setCodes] = useState<AlarmCode[]>([]);
  const [activeAlarms, setActiveAlarms] = useState<Array<{ code: string; desc: string; severity: string; raised_at: number }>>([]);
  const [filters, setFilters] = useState<Record<Severity, boolean>>({ alarm: true, warn: true, info: true, ok: true });
  const [typeFilter, setTypeFilter] = useState("ALL");
  const [loading, setLoading] = useState(true);
  // Set when the latest refresh failed. Cleared on the next success.
  // Industrial-safety norm: a blank feed must never look the same as
  // a healthy feed; an empty result is "no events," an unreachable
  // backend is a different thing and the operator needs to know.
  const [fetchError, setFetchError] = useState<string | null>(null);

  const refresh = async () => {
    setLoading(true);
    try {
      const [e, a, c] = await Promise.all([
        api.events({ limit: 500 }),
        api.alarms(),
        api.alarmCodes(),
      ]);
      setEvents(e.events);
      setActiveAlarms(a.alarms);
      setCodes(c.codes);
      setFetchError(null);
    } catch (err: any) {
      // Preserve the previously-loaded data so the operator still has
      // something to look at; just surface that what they're seeing
      // is no longer fresh.
      setFetchError(err?.message ?? "Failed to load events");
    } finally {
      setLoading(false);
    }
  };
  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 8000);
    return () => clearInterval(t);
  }, []);

  const toggle = (k: Severity) => setFilters((s) => ({ ...s, [k]: !s[k] }));
  const types = ["ALL", ...Array.from(new Set(events.map((e) => e.type)))];
  const list = events.filter((e) => filters[e.severity] && (typeFilter === "ALL" || e.type === typeFilter));

  return (
    <>
      <div className="page-head">
        <div>
          <div className="page-eyebrow">Audit log</div>
          <h1 className="page-title">Events &amp; Alarms</h1>
          <div className="page-sub">
            {list.length} of {events.length} events · {activeAlarms.length} active alarm{activeAlarms.length === 1 ? "" : "s"}
          </div>
        </div>
        <div className="flex ai-c gap-8">
          <button className="btn btn-ghost" onClick={refresh}><Icon name="refresh" size={14} /> Refresh</button>
        </div>
      </div>

      {fetchError && (
        <div
          className="alarm-strip"
          style={{
            background: "color-mix(in oklch, var(--red) 12%, var(--panel-2))",
            borderColor: "color-mix(in oklch, var(--red) 35%, var(--border))",
          }}
        >
          <span className="led" />
          <strong>Events feed unavailable</strong>
          <span>{fetchError} — showing last successful load. Retrying every 8s.</span>
        </div>
      )}

      {activeAlarms.map((a) => (
        <div key={a.code} className="alarm-strip">
          <span className="led" />
          <strong>{a.code}</strong>
          <span>{a.desc}</span>
          <Pill tone="alarm">Active</Pill>
          <button
            className="btn btn-danger"
            onClick={async () => {
              try {
                await api.ackAlarm(a.code);
                refresh();
              } catch (err: any) {
                // Surface ack failures (Modbus write failed, role
                // changed, etc.) so the operator doesn't think they
                // dismissed an alarm they actually didn't.
                setFetchError(
                  err?.body?.detail?.message ??
                  err?.message ??
                  "Failed to acknowledge alarm"
                );
              }
            }}
          >
            Acknowledge
          </button>
        </div>
      ))}

      <Card flush>
        <div className="filters">
          <span className="filter-section-label">Severity</span>
          {ALL_SEVS.map((s) => (
            <button key={s} className="filter-chip" aria-pressed={filters[s]} onClick={() => toggle(s)}>
              <i style={{ width: 6, height: 6, borderRadius: "50%", background: `var(--${sevColor(s)})` }} />
              {capitalize(s)}
            </button>
          ))}
          <span className="filter-sep" />
          <span className="filter-section-label">Type</span>
          {types.map((t) => (
            <button key={t} className="filter-chip" aria-pressed={t === typeFilter} onClick={() => setTypeFilter(t)}>
              {t}
            </button>
          ))}
        </div>
        <div>
          {loading && !events.length ? (
            <EventsSkeleton />
          ) : list.length === 0 ? (
            <EmptyState
              icon="inbox"
              title="No events match these filters"
              desc="Try widening the severity or type filter. Events are kept for the configured audit window."
            />
          ) : (
            list.map((e) => (
              <div key={e.id} className="ev-row" data-sev={e.severity}>
                <span className="ev-time">{relTime(e.ts)}</span>
                <span className="ev-dot" data-sev={e.severity} />
                <span className="ev-type">{e.type}</span>
                <span className="ev-msg">{e.message}</span>
                <span className="ev-meta">{e.meta ?? "—"}</span>
              </div>
            ))
          )}
        </div>
      </Card>

      <Card title="Alarm codes reference" sub="from registers/h100.yaml · edit-then-reload" flush>
        <table className="reg-table">
          <thead><tr><th>Code</th><th>Description</th><th>Severity</th><th>Action</th></tr></thead>
          <tbody>
            {codes.map((c) => (
              <tr key={c.code}>
                <td className="mono">{c.code}</td>
                <td>{c.desc}</td>
                <td><Pill tone={c.severity === "alarm" ? "alarm" : "warn"}>{c.severity}</Pill></td>
                <td className="mono" style={{ color: "var(--text-3)" }}>notify all</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>
    </>
  );
}

function capitalize(s: string) { return s ? s[0].toUpperCase() + s.slice(1) : s; }

function sevColor(s: Severity): string {
  return { alarm: "red", warn: "amber", info: "blue", ok: "green" }[s];
}

function EventsSkeleton() {
  return (
    <div>
      {Array.from({ length: 6 }).map((_, i) => (
        <div key={i} className="ev-row" style={{ alignItems: "center" }}>
          <Skeleton width={72} height={12} />
          <Skeleton width={10} height={10} radius={999} />
          <Skeleton width={74} height={12} />
          <Skeleton width={260 + (i % 3) * 60} height={12} />
          <Skeleton width={68} height={12} />
        </div>
      ))}
    </div>
  );
}
