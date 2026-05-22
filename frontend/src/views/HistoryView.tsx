// History view: range-pickable chart of a single metric, summary stats.

import { useEffect, useMemo, useState } from "react";
import { api } from "../api/client";
import { Card, LineChart, LiveTick, Pill, fmt } from "../components/primitives";

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

        <div className="chart-wrap" style={{ opacity: loading ? 0.7 : 1, transition: "opacity 200ms" }}>
          {data.length >= 2 ? (
            <LineChart series={[{ data, name: m.label }]} colors={[m.color]} xLabels={xLabels} height={320} />
          ) : (
            <div style={{ height: 320, display: "flex", alignItems: "center", justifyContent: "center", color: "var(--text-3)" }}>
              {loading ? "Loading…" : "No telemetry in this range yet — the generator hasn't been polled for that long."}
            </div>
          )}
        </div>

        <div className="grid g-4" style={{ padding: "10px 14px 14px", borderTop: "1px solid var(--border)", gap: 0 }}>
          <StatTile label="min" value={fmt(min, m.digits)} unit={m.unit} />
          <StatTile label="avg" value={fmt(avg, m.digits)} unit={m.unit} />
          <StatTile label="max" value={fmt(max, m.digits)} unit={m.unit} />
          <StatTile label="now" value={fmt(now, m.digits)} unit={m.unit} live />
        </div>
      </Card>
    </>
  );
}

function StatTile({ label, value, unit, live }: { label: string; value: string; unit: string; live?: boolean }) {
  return (
    <div style={{ padding: "10px 14px", borderRight: "1px solid var(--border)" }}>
      <div className="label-row"><span>{label}</span>{live && <LiveTick rateMs={1500} />}</div>
      <div className="mono" style={{ fontSize: 22, fontWeight: 500, marginTop: 4, letterSpacing: "-0.01em" }}>
        {value}<span style={{ fontSize: 12, color: "var(--text-3)", marginLeft: 4, fontWeight: 400 }}>{unit}</span>
      </div>
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
