// Utility-outage history view.
//
// Shows summary stats (count, total duration, kWh delivered) over 30 d
// and 365 d, plus a per-outage table with start/end, peak kW, kWh, and
// an editable notes field. An open (in-progress) outage shows a "LIVE"
// chip and ticks the duration in real time.

import { useEffect, useMemo, useState } from "react";
import { api, ApiError } from "../api/client";
import { Card, EmptyState, Icon, Pill, Skeleton } from "../components/primitives";
import type { Outage, OutageSummary } from "../types";
import { relTime } from "./LiveView";

export function OutagesView() {
  const [outages, setOutages] = useState<Outage[]>([]);
  const [s30, setS30] = useState<OutageSummary | null>(null);
  const [s365, setS365] = useState<OutageSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [draftNote, setDraftNote] = useState("");
  const [now, setNow] = useState(Date.now() / 1000);

  const refresh = async () => {
    setLoading(true);
    try {
      const r = await api.outages({ limit: 500 });
      setOutages(r.outages);
      setS30(r.summary30d);
      setS365(r.summary365d);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 30_000);
    const tick = setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => { clearInterval(t); clearInterval(tick); };
  }, []);

  const beginEdit = (o: Outage) => {
    setEditingId(o.id);
    setDraftNote(o.notes);
  };

  const saveNote = async (o: Outage) => {
    try {
      await api.annotateOutage(o.id, draftNote);
      setEditingId(null);
      await refresh();
    } catch (e: any) {
      window.alert(
        e instanceof ApiError ? `Save failed: HTTP ${e.status}` : `Save failed: ${e?.message}`,
      );
    }
  };

  const stats = useMemo(() => {
    if (!s30 || !s365) return null;
    return {
      h30: s30,
      h365: s365,
      hasOpen: s30.open > 0,
    };
  }, [s30, s365]);

  return (
    <>
      <div className="page-head">
        <div>
          <div className="page-eyebrow">Utility · outage history</div>
          <h1 className="page-title">Outages</h1>
          <div className="page-sub">
            Auto-detected from engine-state transitions while the panel is in AUTO.
            Each outage tracks duration, peak kW, and delivered kWh.
          </div>
        </div>
        <div className="flex ai-c gap-8">
          {stats?.hasOpen && <Pill tone="alarm">Outage in progress</Pill>}
          <button className="btn btn-ghost" onClick={refresh}>
            <Icon name="refresh" size={14} /> Refresh
          </button>
        </div>
      </div>

      {stats && (
        <div className="row" style={{ marginBottom: "var(--gap)" }}>
          <SummaryCard title="Last 30 days" s={stats.h30} />
          <SummaryCard title="Last 365 days" s={stats.h365} />
        </div>
      )}

      <Card title="Outage log" sub={`${outages.length} events`} flush>
        {loading && !outages.length ? (
          <OutagesLoadingSkeleton />
        ) : outages.length === 0 ? (
          <EmptyState
            icon="bolt"
            title="No outages recorded"
            desc="Outages are logged automatically when the engine starts in AUTO mode. None to date — your utility's behaving."
          />
        ) : (
          <table className="reg-table">
            <thead>
              <tr>
                <th>Started</th>
                <th>Ended</th>
                <th>Duration</th>
                <th>Peak kW</th>
                <th>kWh delivered</th>
                <th>Notes</th>
                <th style={{ textAlign: "right" }}>Actions</th>
              </tr>
            </thead>
            <tbody>
              {outages.map((o) => {
                const liveDuration = o.endedTs == null ? Math.max(0, now - o.startedTs) : o.durationS ?? 0;
                return (
                  <tr key={o.id}>
                    <td className="mono">{relTime(o.startedTs)}</td>
                    <td className="mono">
                      {o.endedTs == null ? <Pill tone="alarm">LIVE</Pill> : relTime(o.endedTs)}
                    </td>
                    <td className="mono">{formatDuration(liveDuration)}</td>
                    <td className="mono">{o.peakKw.toFixed(1)}</td>
                    <td className="mono">{o.kwh.toFixed(2)}</td>
                    <td>
                      {editingId === o.id ? (
                        <input
                          className="input"
                          value={draftNote}
                          autoFocus
                          onChange={(e) => setDraftNote(e.target.value)}
                          onKeyDown={(e) => { if (e.key === "Enter") saveNote(o); }}
                        />
                      ) : (
                        <span style={{ color: o.notes ? "var(--text)" : "var(--text-3)" }}>
                          {o.notes || "—"}
                        </span>
                      )}
                    </td>
                    <td style={{ textAlign: "right" }}>
                      {editingId === o.id ? (
                        <>
                          <button className="btn btn-ghost" onClick={() => setEditingId(null)}>Cancel</button>{" "}
                          <button className="btn btn-primary" onClick={() => saveNote(o)}>Save</button>
                        </>
                      ) : (
                        <button className="btn btn-ghost" onClick={() => beginEdit(o)}>
                          Edit notes
                        </button>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </Card>
    </>
  );
}

function SummaryCard({ title, s }: { title: string; s: OutageSummary }) {
  return (
    <Card title={title} sub={`${s.count} outage${s.count === 1 ? "" : "s"}`}>
      <div className="grid g-2" style={{ gap: 14 }}>
        <Stat label="Total runtime" value={formatDuration(s.totalDurationS)} />
        <Stat label="Total kWh" value={`${s.totalKwh.toFixed(2)} kWh`} />
        <Stat label="Peak kW" value={`${s.peakKw.toFixed(1)} kW`} />
        <Stat label="Longest" value={formatDuration(s.longestDurationS)} />
      </div>
    </Card>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="label-row"><span>{label}</span></div>
      <div className="mono" style={{ fontSize: 20, fontWeight: 500, marginTop: 4 }}>{value}</div>
    </div>
  );
}

function formatDuration(s: number): string {
  if (s < 60) return `${Math.floor(s)}s`;
  if (s < 3600) {
    const m = Math.floor(s / 60);
    const sec = Math.floor(s % 60);
    return `${m}m ${sec}s`;
  }
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  return `${h}h ${m}m`;
}

function OutagesLoadingSkeleton() {
  return (
    <div style={{ padding: 14 }}>
      {Array.from({ length: 5 }).map((_, i) => (
        <div
          key={i}
          style={{ display: "grid", gridTemplateColumns: "1fr 1fr 100px 80px 100px 1fr 80px", gap: 12, padding: "10px 0" }}
        >
          <Skeleton width="80%" height={14} />
          <Skeleton width="80%" height={14} />
          <Skeleton width={80} height={14} />
          <Skeleton width={60} height={14} />
          <Skeleton width={80} height={14} />
          <Skeleton width="70%" height={14} />
          <Skeleton width={70} height={14} />
        </div>
      ))}
    </div>
  );
}
