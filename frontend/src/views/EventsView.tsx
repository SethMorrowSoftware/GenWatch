// Events + alarms view: filter chips, event log, alarm codes reference.

import { useEffect, useState } from "react";
import { api } from "../api/client";
import { Card, Icon, Pill } from "../components/primitives";
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
          <h1 className="page-title">Events &amp; Alarms</h1>
          <div className="page-sub">
            {list.length} of {events.length} events · {activeAlarms.length} active alarm{activeAlarms.length === 1 ? "" : "s"}
          </div>
        </div>
        <div className="flex ai-c gap-8">
          <button className="btn btn-ghost" onClick={refresh}><Icon name="refresh" size={14} /> Refresh</button>
        </div>
      </div>

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
              } catch { /* ignore */ }
            }}
          >
            Acknowledge
          </button>
        </div>
      ))}

      <Card flush>
        <div className="filters">
          <span style={{
            color: "var(--text-3)", fontSize: 11, textTransform: "uppercase",
            letterSpacing: "0.08em", marginRight: 4, alignSelf: "center",
          }}>Severity</span>
          {ALL_SEVS.map((s) => (
            <button key={s} className="filter-chip" aria-pressed={filters[s]} onClick={() => toggle(s)}>
              <i style={{ width: 6, height: 6, borderRadius: "50%", background: `var(--${sevColor(s)})` }} />
              {capitalize(s)}
            </button>
          ))}
          <span style={{ width: 1, height: 16, background: "var(--border)", margin: "0 4px", alignSelf: "center" }} />
          <span style={{
            color: "var(--text-3)", fontSize: 11, textTransform: "uppercase",
            letterSpacing: "0.08em", marginRight: 4, alignSelf: "center",
          }}>Type</span>
          {types.map((t) => (
            <button key={t} className="filter-chip" aria-pressed={t === typeFilter} onClick={() => setTypeFilter(t)}>
              {t}
            </button>
          ))}
        </div>
        <div>
          {loading && !events.length ? (
            <div style={{ padding: 14, color: "var(--text-3)", fontSize: 12 }}>Loading events…</div>
          ) : (
            list.map((e) => (
              <div key={e.id} className="ev-row">
                <span className="ev-time">{relTime(e.ts)}</span>
                <span className="ev-dot" data-sev={e.severity} />
                <span className="ev-type">{e.type}</span>
                <span className="ev-msg" dangerouslySetInnerHTML={{ __html: e.message }} />
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
