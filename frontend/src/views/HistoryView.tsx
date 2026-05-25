// History view: range-pickable chart of a single metric, summary stats.

import { useEffect, useMemo, useState } from "react";
import { api } from "../api/client";
import { Card, EmptyState, LineChart, LiveTick, Pill, Skeleton, fmt } from "../components/primitives";

type MetricKey = "kw" | "rpm" | "hz" | "oilP" | "coolT" | "batt" | "vAB" | "iA";
type RangeKey = "10M" | "1H" | "6H" | "24H" | "7D" | "30D";

const METRICS: Record<MetricKey, { label: string; unit: string; color: string; digits: number }> = {
  kw:    { label: "Real power",   unit: "kW",  color: "var(--amber)", digits: 0 },
  rpm:   { label: "Engine RPM",   unit: "rpm", color: "var(--green)", digits: 0 },
  hz:    { label: "Frequency",    unit: "Hz",  color: "var(--green)", digits: 2 },
  oilP:  { label: "Oil pressure", unit: "psi", color: "var(--blue)",  digits: 0 },
  coolT: { label: "Coolant temp", unit: "°F",  color: "var(--amber)", digits: 0 },
  batt:  { label: "Battery",      unit: "V",   color: "var(--text-2)", digits: 2 },
  vAB:   { label: "Voltage A–B",  unit: "V",   color: "var(--green)", digits: 0 },
  iA:    { label: "Current A",    unit: "A",   color: "var(--blue)",  digits: 0 },
};

const RANGES: Record<RangeKey, number> = {
  "10M": 10 * 60,
  "1H": 3600,
  "6H": 6 * 3600,
  "24H": 24 * 3600,
  "7D": 7 * 86400,
  "30D": 30 * 86400,
};

export function HistoryView() {
  const [range, setRange] = useState<RangeKey>("1H");
  const [metric, setMetric] = useState<MetricKey>("kw");
  const [points, setPoints] = useState<[number, number][]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    const now = Date.now() / 1000;
    const from = now - RANGES[range];
    api.telemetry({ metric, from, to: now, maxPoints: 1200 })
      .then((r) => { if (!cancelled) setPoints(r.points); })
      .catch(() => { if (!cancelled) setPoints([]); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [metric, range]);

  const m = METRICS[metric];
  const data = useMemo(() => points.map(([_, v]) => v), [points]);

  const min = data.length ? Math.min(...data) : 0;
  const max = data.length ? Math.max(...data) : 0;
  const avg = data.length ? data.reduce((a, b) => a + b, 0) / data.length : 0;
  const now = data.length ? data[data.length - 1] : 0;

  const xLabels = useMemo(() => buildXLabels(points, 8), [points]);

  return (
    <>
      <div className="page-head">
        <div>
          <div className="page-eyebrow">Time-series · {range}</div>
          <h1 className="page-title">Telemetry History</h1>
          <div className="page-sub">SQLite time-series · raw 7 d, 1-min rollup 90 d · {Object.keys(METRICS).length} indexed metrics</div>
        </div>
        <div className="flex ai-c gap-8">
          <Pill tone="info">{data.length} samples</Pill>
        </div>
      </div>

      <Card flush>
        <div className="chart-toolbar">
          <div className="flex ai-c gap-12">
            <div className="range-group">
              {(Object.keys(RANGES) as RangeKey[]).map((r) => (
                <button key={r} aria-current={r === range ? "page" : undefined} onClick={() => setRange(r)}>{r}</button>
              ))}
            </div>
            <span style={{ color: "var(--text-3)", fontSize: 12, fontFamily: "var(--mono)" }}>
              {rangeLabel(range, points)}
            </span>
          </div>
          <div className="legend"><i style={{ background: m.color }} /> {m.label}</div>
        </div>

        <div className="metric-tabs">
          {(Object.entries(METRICS) as [MetricKey, typeof m][]).map(([k, def]) => (
            <button key={k} className="metric-tab" aria-current={k === metric ? "true" : undefined} onClick={() => setMetric(k)}>
              <span className="swatch" style={{ background: def.color }} />
              {def.label}
            </button>
          ))}
        </div>

        <div className="chart-wrap" style={{ opacity: loading && data.length >= 2 ? 0.5 : 1, transition: "opacity 200ms" }}>
          {data.length >= 2 ? (
            <LineChart series={[{ data, name: m.label }]} colors={[m.color]} xLabels={xLabels} height={320} />
          ) : loading ? (
            <div style={{ height: 320, padding: "16px 4px", display: "flex", flexDirection: "column", justifyContent: "space-between" }}>
              <div style={{ display: "flex", justifyContent: "space-between", padding: "0 16px 8px" }}>
                <Skeleton width={48} height={12} />
                <Skeleton width={64} height={12} />
              </div>
              <Skeleton width="100%" height={220} radius={8} />
              <div style={{ display: "flex", justifyContent: "space-between", padding: "8px 16px 0", gap: 8 }}>
                {Array.from({ length: 6 }).map((_, i) => <Skeleton key={i} width={40} height={10} />)}
              </div>
            </div>
          ) : (
            <div style={{ height: 320, display: "flex", alignItems: "center", justifyContent: "center" }}>
              <EmptyState
                icon="spark"
                title="No telemetry in this range yet"
                desc="The generator hasn't been polled for this duration. Try a shorter range, or come back after the next poll."
              />
            </div>
          )}
        </div>

        <div className="grid g-4" style={{ padding: "10px 0", borderTop: "1px solid var(--border)", gap: 0 }}>
          <StatTile label="min" value={fmt(min, m.digits)} unit={m.unit} loading={loading && !data.length} />
          <StatTile label="avg" value={fmt(avg, m.digits)} unit={m.unit} loading={loading && !data.length} />
          <StatTile label="max" value={fmt(max, m.digits)} unit={m.unit} loading={loading && !data.length} />
          <StatTile label="now" value={fmt(now, m.digits)} unit={m.unit} live loading={loading && !data.length} />
        </div>
      </Card>
    </>
  );
}

function StatTile({ label, value, unit, live, loading }: { label: string; value: string; unit: string; live?: boolean; loading?: boolean }) {
  return (
    <div style={{ padding: "12px 16px", borderRight: "1px solid var(--border)" }}>
      <div className="label-row"><span>{label}</span>{live && <LiveTick rateMs={1500} />}</div>
      {loading ? (
        <div style={{ marginTop: 6 }}><Skeleton width={80} height={22} /></div>
      ) : (
        <div className="mono" style={{ fontSize: 22, fontWeight: 500, marginTop: 4, letterSpacing: "-0.01em" }}>
          {value}<span style={{ fontSize: 12, color: "var(--text-3)", marginLeft: 4, fontWeight: 400 }}>{unit}</span>
        </div>
      )}
    </div>
  );
}

function buildXLabels(points: [number, number][], count: number): string[] {
  if (points.length < 2) return [];
  const labels: string[] = [];
  for (let i = 0; i < count; i++) {
    const idx = Math.floor((points.length - 1) * (i / (count - 1)));
    const ts = points[idx][0];
    if (i === count - 1) labels.push("now");
    else labels.push(formatClock(ts));
  }
  return labels;
}

function formatClock(ts: number): string {
  const d = new Date(ts * 1000);
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

function rangeLabel(range: RangeKey, points: [number, number][]): string {
  if (!points.length) return range;
  const a = new Date(points[0][0] * 1000);
  const b = new Date(points[points.length - 1][0] * 1000);
  return `${formatClock(a.getTime() / 1000)} — ${formatClock(b.getTime() / 1000)}`;
}
