// Live dashboard: hero, electrical, controls, engine, fuel, events.

import { useEffect, useState } from "react";
import { api } from "../api/client";
import { Card, EmptyState, Icon, LiveTick, Pill, Sparkline, fmt, formatTimeInState } from "../components/primitives";
import type { ActiveAlarm, EngineState, EventRow, Reading, StatusBody } from "../types";
import { ConfirmModal } from "./ConfirmModal";

interface Props {
  status: StatusBody;
  history: Reading[];
  operator: string;
}

const STATE_LABEL: Record<EngineState, string> = {
  stopped: "Stopped",
  cranking: "Cranking",
  running: "Running",
  exercising: "Exercising",
  cooling: "Cooling",
  alarm: "Alarm",
  unknown: "Unknown",
};
const STATE_SUB: Record<EngineState, string> = {
  stopped: "AUTO · Ready",
  cranking: "Engine start in progress",
  running: "On load · Utility lost",
  exercising: "Quiet-Test · No load",
  cooling: "Engine cool-down",
  alarm: "Shutdown · Operator action required",
  unknown: "—",
};
const STATE_BADGE: Record<EngineState, string> = {
  stopped: "STOPPED",
  cranking: "CRANKING",
  running: "ON LOAD",
  exercising: "EXERCISING",
  cooling: "COOLING",
  alarm: "ALARM",
  unknown: "—",
};

export function LiveView({ status, history, operator }: Props) {
  const [confirmCmd, setConfirmCmd] = useState<"start" | "stop" | "exercise" | "transfer" | null>(null);

  const reading = status.reading;
  const alarm: ActiveAlarm | undefined = status.activeAlarms[0];

  return (
    <>
      <div className="page-head">
        <div>
          <h1 className="page-title">Site overview</h1>
          <div className="page-sub">
            {status.site.id} · {status.site.name} · {status.site.ratingKw} kW · {status.site.engine} ·
            last sync <span className="mono">{(status.comms.rateMs / 1000).toFixed(1)} s ago</span>
          </div>
        </div>
        <div className="flex ai-c gap-8">
          {status.exercise.enabled && (
            <Pill tone="info">
              Auto · {status.exercise.time} {capitalize(status.exercise.day)} exercise
            </Pill>
          )}
        </div>
      </div>

      {alarm && <AlarmStrip alarm={alarm} />}

      <StatusHero status={status} />

      <div className="row" style={{ marginTop: "var(--gap)" }}>
        <ElectricalCard reading={reading} history={history} rateMs={status.comms.rateMs} />
        <ControlsPanel state={status.state} onCommand={setConfirmCmd} />
      </div>

      <div className="row" style={{ marginTop: "var(--gap)" }}>
        <EngineCard reading={reading} history={history} />
        <FuelMaintCard reading={reading} status={status} />
      </div>

      <div style={{ marginTop: "var(--gap)" }}>
        <EventsFeed limit={6} />
      </div>

      <ConfirmModal
        command={confirmCmd}
        operator={operator}
        onClose={() => setConfirmCmd(null)}
        onSuccess={() => setConfirmCmd(null)}
      />
    </>
  );
}

function capitalize(s: string) {
  return s ? s[0].toUpperCase() + s.slice(1) : s;
}

// ─── Status hero ──────────────────────────────────────────────────────────
function StatusHero({ status }: { status: StatusBody }) {
  const state = status.state;
  const r = status.reading;
  return (
    <div className="hero" data-state={state}>
      <div className="hero-left">
        <span className="state-badge">
          <i className="led" />
          {STATE_BADGE[state]}
        </span>
        <div className="state-title">{STATE_LABEL[state]}</div>
        <div className="state-sub">
          {STATE_SUB[state]} · <span className="state-time mono">{formatTimeInState(status.timeInState)}</span>
        </div>
        <div className="hero-stats">
          <div className="hero-stat">
            <div className="l">Frequency</div>
            <div className="v">{r.hz != null ? r.hz.toFixed(1) : "0.0"}<span className="u">Hz</span></div>
          </div>
          <div className="hero-stat">
            <div className="l">Real power</div>
            <div className="v">{fmt(r.kw)}<span className="u">kW</span></div>
          </div>
          <div className="hero-stat">
            <div className="l">Engine RPM</div>
            <div className="v">{fmt(r.rpm)}<span className="u">rpm</span></div>
          </div>
        </div>
      </div>
      <div className="hero-right">
        <AtsPanel status={status} />
      </div>
    </div>
  );
}

// ─── ATS panel ────────────────────────────────────────────────────────────
function AtsPanel({ status }: { status: StatusBody }) {
  const onGen = status.hts.transferredToGen;
  const r = status.reading;
  const loadPct = r.kw != null ? Math.round((r.kw / Math.max(1, status.site.ratingKw)) * 100) : 0;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
      <div className="flex jc-sb ai-c">
        <div>
          <div style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: "0.08em", color: "var(--text-3)" }}>
            HTS-1 Transfer Switch
          </div>
          <div style={{ fontSize: 13.5, color: "var(--text)", marginTop: 4, fontWeight: 500 }}>
            Source: <span style={{ color: onGen ? "var(--green)" : "var(--blue)" }}>{onGen ? "Generator" : "Utility"}</span>
          </div>
        </div>
        <Pill tone={status.state === "alarm" ? "alarm" : onGen ? "ok" : "info"}>
          {onGen ? "Transferred" : "Normal"}
        </Pill>
      </div>

      <div style={{ position: "relative", padding: "4px 0" }}>
        <svg width="100%" height="120" viewBox="0 0 320 120" preserveAspectRatio="none" style={{ display: "block" }}>
          <rect x="6" y="14" width="80" height="36" rx="6" fill="var(--panel)" stroke={!onGen ? "var(--blue-d)" : "var(--border)"} />
          <text x="46" y="32" textAnchor="middle" fontSize="9" fontFamily="Geist" fill="var(--text-3)" letterSpacing="1">UTILITY</text>
          <text x="46" y="44" textAnchor="middle" fontSize="11" fontFamily="JetBrains Mono" fill="var(--text)">480 V</text>

          <rect x="6" y="70" width="80" height="36" rx="6" fill="var(--panel)" stroke={onGen ? "var(--green-d)" : "var(--border)"} />
          <text x="46" y="88" textAnchor="middle" fontSize="9" fontFamily="Geist" fill="var(--text-3)" letterSpacing="1">GENERATOR</text>
          <text x="46" y="100" textAnchor="middle" fontSize="11" fontFamily="JetBrains Mono" fill="var(--text)">
            {r.vAB != null ? Math.round(r.vAB) : 0} V
          </text>

          <line x1="86" y1="32" x2="140" y2="32" stroke={!onGen ? "var(--blue)" : "var(--border-2)"} strokeWidth="1.5" />
          <line x1="86" y1="88" x2="140" y2="88" stroke={onGen ? "var(--green)" : "var(--border-2)"} strokeWidth="1.5" />

          <rect x="140" y="20" width="60" height="80" rx="6" fill="var(--panel-2)" stroke="var(--border-2)" />
          <text x="170" y="36" textAnchor="middle" fontSize="9" fontFamily="Geist" fill="var(--text-3)" letterSpacing="1">ATS</text>
          <circle cx="155" cy="60" r="2.5" fill={!onGen ? "var(--blue)" : "var(--text-4)"} />
          <circle cx="155" cy="78" r="2.5" fill={onGen ? "var(--green)" : "var(--text-4)"} />
          <circle cx="185" cy="60" r="2.5" fill="var(--text-2)" />
          <line x1="155" y1={onGen ? 78 : 60} x2="185" y2="60" stroke={onGen ? "var(--green)" : "var(--blue)"} strokeWidth="2" strokeLinecap="round" />

          <line x1="200" y1="60" x2="260" y2="60" stroke="var(--green)" strokeWidth="1.5" />

          <rect x="260" y="42" width="54" height="36" rx="6" fill="var(--panel)" stroke="var(--border-2)" />
          <text x="287" y="60" textAnchor="middle" fontSize="9" fontFamily="Geist" fill="var(--text-3)" letterSpacing="1">LOAD</text>
          <text x="287" y="73" textAnchor="middle" fontSize="11" fontFamily="JetBrains Mono" fill="var(--text)">{fmt(r.kw)} kW</text>
        </svg>
      </div>

      <div className="grid g-3" style={{ gap: 10 }}>
        <div className="hero-stat" style={{ border: "1px solid var(--border)", borderRadius: 7, background: "var(--panel)" }}>
          <div className="l">Load</div>
          <div className="v">{loadPct}<span className="u">%</span></div>
        </div>
        <div className="hero-stat" style={{ border: "1px solid var(--border)", borderRadius: 7, background: "var(--panel)" }}>
          <div className="l">Last transfer</div>
          <div className="v" style={{ fontSize: 14 }}>—</div>
        </div>
        <div className="hero-stat" style={{ border: "1px solid var(--border)", borderRadius: 7, background: "var(--panel)" }}>
          <div className="l">Transfers (30d)</div>
          <div className="v" style={{ fontSize: 14 }}>—</div>
        </div>
      </div>
    </div>
  );
}

// ─── Alarm strip ──────────────────────────────────────────────────────────
function AlarmStrip({ alarm }: { alarm: ActiveAlarm }) {
  const [ack, setAck] = useState(false);
  const onAck = async () => {
    setAck(true);
    try {
      await api.ackAlarm(alarm.code);
    } catch (e) {
      setAck(false);
    }
  };
  const ago = Math.floor((Date.now() / 1000 - alarm.raised_at));
  const agoStr = `${Math.floor(ago / 60)}m ${ago % 60}s`;
  return (
    <div className="alarm-strip" role="alert">
      <span className="led" />
      <strong>Active alarm</strong>
      <span>{alarm.desc} · raised {agoStr} ago</span>
      <button className="btn btn-danger" disabled={ack} onClick={onAck}>
        {ack ? "Acknowledged" : "Acknowledge"}
      </button>
    </div>
  );
}

// ─── Phase row ────────────────────────────────────────────────────────────
function PhaseRow({ label, value, unit, pct, color }: { label: string; value: string; unit: string; pct: number; color: string }) {
  return (
    <div className="phase-row">
      <div className="ph">{label}</div>
      <div className="bar"><i style={{ width: `${Math.max(0, Math.min(100, pct))}%`, background: color }} /></div>
      <div className="num">{value}<span className="u">{unit}</span></div>
    </div>
  );
}

// ─── Electrical card ──────────────────────────────────────────────────────
function ElectricalCard({ reading: r, history, rateMs }: { reading: Reading; history: Reading[]; rateMs: number }) {
  const vMax = 500, iMax = 240;
  const vAvg = avgOf([r.vAB, r.vBC, r.vCA]);
  const iAvg = avgOf([r.iA, r.iB, r.iC]);
  return (
    <Card title="Generator Output" sub="3-phase · Line-to-Line" actions={<LiveTick rateMs={rateMs} />}>
      <div className="grid g-2" style={{ gap: 16 }}>
        <div>
          <div className="label-row" style={{ marginBottom: 6 }}>
            <span>Voltage L–L</span>
            <span className="mono" style={{ textTransform: "none", letterSpacing: 0 }}>
              {Math.round(vAvg)} V <span style={{ color: "var(--text-4)" }}>avg</span>
            </span>
          </div>
          <PhaseRow label="A–B" value={fmt(r.vAB)} unit=" V" pct={(r.vAB ?? 0) / vMax * 100} color="var(--green)" />
          <PhaseRow label="B–C" value={fmt(r.vBC)} unit=" V" pct={(r.vBC ?? 0) / vMax * 100} color="var(--amber)" />
          <PhaseRow label="C–A" value={fmt(r.vCA)} unit=" V" pct={(r.vCA ?? 0) / vMax * 100} color="var(--blue)" />
          <div style={{ marginTop: 10, opacity: 0.85 }}>
            <Sparkline points={history.map((h) => h.vAB ?? 0).reverse()} width={300} height={36} color="var(--text-3)" />
          </div>
        </div>
        <div>
          <div className="label-row" style={{ marginBottom: 6 }}>
            <span>Current per phase</span>
            <span className="mono" style={{ textTransform: "none", letterSpacing: 0 }}>
              {Math.round(iAvg)} A <span style={{ color: "var(--text-4)" }}>avg</span>
            </span>
          </div>
          <PhaseRow label="A" value={fmt(r.iA)} unit=" A" pct={(r.iA ?? 0) / iMax * 100} color="var(--green)" />
          <PhaseRow label="B" value={fmt(r.iB)} unit=" A" pct={(r.iB ?? 0) / iMax * 100} color="var(--amber)" />
          <PhaseRow label="C" value={fmt(r.iC)} unit=" A" pct={(r.iC ?? 0) / iMax * 100} color="var(--blue)" />
          <div style={{ marginTop: 10, opacity: 0.85 }}>
            <Sparkline points={history.map((h) => h.iA ?? 0).reverse()} width={300} height={36} color="var(--text-3)" />
          </div>
        </div>
      </div>

      <div className="grid g-3" style={{ gap: 12, marginTop: 16, paddingTop: 14, borderTop: "1px solid var(--border)" }}>
        <BigMetric label="Frequency" value={r.hz != null ? r.hz.toFixed(1) : "—"} unit="Hz"
                   tone={r.hz != null && Math.abs(r.hz - 60) < 0.5 ? "ok" : "warn"}
                   sparkPoints={history.map((h) => h.hz ?? 0).reverse()} sparkColor="var(--green)" />
        <BigMetric label="Real power" value={fmt(r.kw)} unit="kW"
                   sparkPoints={history.map((h) => h.kw ?? 0).reverse()} sparkColor="var(--amber)" />
        <BigMetric label="Apparent" value={fmt(r.kw != null ? Math.round(r.kw * 1.07) : null)} unit="kVA"
                   sparkPoints={history.map((h) => (h.kw ?? 0) * 1.07).reverse()} sparkColor="var(--blue)" />
      </div>
    </Card>
  );
}

function BigMetric({ label, value, unit, tone, sparkPoints, sparkColor }: {
  label: string; value: string; unit: string; tone?: "ok" | "warn";
  sparkPoints: number[]; sparkColor: string;
}) {
  return (
    <div>
      <div className="label-row">
        <span>{label}</span>
        {tone && <Pill tone={tone}>{tone === "ok" ? "in band" : "review"}</Pill>}
      </div>
      <div className="mono" style={{ fontSize: 26, fontWeight: 500, marginTop: 6, letterSpacing: "-0.01em" }}>
        {value}<span style={{ fontSize: 13, color: "var(--text-3)", marginLeft: 4, fontWeight: 400 }}>{unit}</span>
      </div>
      <div style={{ marginTop: 6, opacity: 0.85 }}>
        <Sparkline points={sparkPoints} width={220} height={32} color={sparkColor} />
      </div>
    </div>
  );
}

function avgOf(xs: Array<number | null>): number {
  const vs = xs.filter((x) => x != null) as number[];
  return vs.length ? vs.reduce((a, b) => a + b, 0) / vs.length : 0;
}

// ─── Engine card ──────────────────────────────────────────────────────────
function EngineCard({ reading: r, history }: { reading: Reading; history: Reading[] }) {
  return (
    <Card title="Engine" sub="Cummins QSB7-G5"
          actions={<Pill tone={r.oilP != null && r.oilP < 25 && (r.rpm ?? 0) > 100 ? "alarm" : "ok"}>nominal</Pill>}>
      <div className="grid g-4">
        <EngineMetric label="RPM"        value={fmt(r.rpm)}            unit="rpm" sparkPoints={history.map((h) => h.rpm ?? 0).reverse()} color="var(--green)" warnRange={[1750, 1850]} numeric={r.rpm} min={0} max={2200} />
        <EngineMetric label="Oil pres."  value={r.oilP != null ? r.oilP.toFixed(0) : "—"} unit="psi" sparkPoints={history.map((h) => h.oilP ?? 0).reverse()} color="var(--blue)"  warnRange={[35, 80]} numeric={r.oilP} min={0} max={100} />
        <EngineMetric label="Coolant"    value={r.coolT != null ? r.coolT.toFixed(0) : "—"} unit="°F" sparkPoints={history.map((h) => h.coolT ?? 0).reverse()} color="var(--amber)" warnRange={[170, 210]} numeric={r.coolT} min={50} max={250} />
        <EngineMetric label="Battery"    value={r.batt != null ? r.batt.toFixed(2) : "—"} unit="V" sparkPoints={history.map((h) => h.batt ?? 0).reverse()} color="var(--text-2)" warnRange={[12.6, 14.4]} numeric={r.batt} min={10} max={16} />
      </div>
    </Card>
  );
}

function EngineMetric({ label, value, unit, sparkPoints, color, warnRange, numeric }: {
  label: string; value: string; unit: string;
  sparkPoints: number[]; color: string; warnRange?: [number, number];
  numeric: number | null; min: number; max: number;
}) {
  const inBand = numeric != null && warnRange ? numeric >= warnRange[0] && numeric <= warnRange[1] : true;
  return (
    <div style={{ padding: "2px 4px" }}>
      <div className="label-row">
        <span>{label}</span>
        {warnRange && (
          <span style={{ textTransform: "none", letterSpacing: 0, color: inBand ? "var(--text-4)" : "var(--amber)" }} className="mono">
            {warnRange[0]}–{warnRange[1]}
          </span>
        )}
      </div>
      <div className="mono" style={{ fontSize: 26, fontWeight: 500, marginTop: 6, letterSpacing: "-0.01em",
                                     color: inBand ? "var(--text)" : "var(--amber)" }}>
        {value}<span style={{ fontSize: 13, color: "var(--text-3)", marginLeft: 4, fontWeight: 400 }}>{unit}</span>
      </div>
      <div style={{ marginTop: 4 }}>
        <Sparkline points={sparkPoints} width={200} height={32} color={color} />
      </div>
    </div>
  );
}

// ─── Controls panel ──────────────────────────────────────────────────────
function ControlsPanel({ state, onCommand }: {
  state: EngineState; onCommand: (cmd: "start" | "stop" | "exercise" | "transfer") => void;
}) {
  const canStart = state === "stopped";
  const canStop = state === "running" || state === "exercising" || state === "cranking";
  const canExercise = state === "stopped";
  const canTransfer = state === "running";
  return (
    <Card title="Controls" sub="Operator · two-step confirm">
      <div className="ctl-stack">
        <button className="ctl-btn" data-tone="start" disabled={!canStart} onClick={() => onCommand("start")}>
          <span className="icon"><Icon name="play" size={16} /></span>
          <span><div className="lbl">Remote Start</div><div className="desc">Crank engine, transfer to gen</div></span>
          <span className="kbd">⌘S</span>
        </button>
        <button className="ctl-btn" data-tone="stop" disabled={!canStop} onClick={() => onCommand("stop")}>
          <span className="icon"><Icon name="stop" size={14} /></span>
          <span><div className="lbl">Remote Stop</div><div className="desc">Cool-down then engine-off</div></span>
          <span className="kbd">⌘.</span>
        </button>
        <button className="ctl-btn" data-tone="exer" disabled={!canExercise} onClick={() => onCommand("exercise")}>
          <span className="icon"><Icon name="activity" size={16} /></span>
          <span><div className="lbl">Quiet-Test</div><div className="desc">Run unloaded · 30 min default</div></span>
          <span className="kbd">⌘E</span>
        </button>
        <button className="ctl-btn" data-tone="xfer" disabled={!canTransfer} onClick={() => onCommand("transfer")}>
          <span className="icon"><Icon name="switch_" size={18} /></span>
          <span><div className="lbl">Transfer back</div><div className="desc">HTS-1 → Utility, cool engine</div></span>
          <span className="kbd">⌘T</span>
        </button>
      </div>
      <div style={{ marginTop: 14, padding: "10px 12px", background: "var(--panel-2)", borderRadius: 8,
                    border: "1px solid var(--border)", fontSize: 12, color: "var(--text-3)",
                    display: "flex", gap: 10, alignItems: "flex-start" }}>
        <Icon name="lock" size={14} />
        <div>Commands write to <span className="mono" style={{ color: "var(--text-2)" }}>0x00A0–A3</span> via FC06.
             Engine hardware safeties (panel) remain primary.</div>
      </div>
    </Card>
  );
}

// ─── Fuel + maintenance card ─────────────────────────────────────────────
function FuelMaintCard({ reading: r, status }: { reading: Reading; status: StatusBody }) {
  const fuel = r.fuelPct ?? 0;
  const lowFuel = fuel < 25;
  const gal = Math.round(fuel * (status.site.tankGal / 100));
  return (
    <Card title="Tank · Maintenance" sub={`Local diesel · ${status.site.tankGal} gal`}>
      <div style={{ padding: "4px 0 12px" }}>
        <div className="label-row" style={{ padding: "0 0 8px" }}>
          <span>Fuel level</span>
          <span className="mono" style={{ textTransform: "none", letterSpacing: 0, color: lowFuel ? "var(--amber)" : "var(--text-2)" }}>
            {fuel.toFixed(1)} % · ~{gal} gal
          </span>
        </div>
        <div className="fuel-bar">
          <i style={{ width: `${fuel}%` }} data-low={lowFuel ? "1" : "0"} />
          <div className="ticks">
            {Array.from({ length: 9 }).map((_, i) => <i key={i} />)}
          </div>
        </div>
        <div className="flex jc-sb mono" style={{ marginTop: 6, fontSize: 10.5, color: "var(--text-4)" }}>
          <span>0</span><span>25</span><span>50</span><span>75</span><span>100 %</span>
        </div>
      </div>

      <div className="kv" style={{ marginTop: 8, paddingTop: 10, borderTop: "1px solid var(--border)" }}>
        <div className="kv-row"><span className="l">Run hours (total)</span>
          <span className="v">{fmt(r.runHours, 1)} h</span></div>
        <div className="kv-row"><span className="l">Engine starts</span>
          <span className="v">{fmt(r.startCount)}</span></div>
        <div className="kv-row"><span className="l">Next exercise</span>
          <span className="v">{capitalize(status.exercise.day)} · {status.exercise.time}</span></div>
        <div className="kv-row"><span className="l">Last alarm</span>
          <span className="v">
            {status.activeAlarms[0] ? status.activeAlarms[0].desc : "—"}
          </span></div>
      </div>
    </Card>
  );
}

// ─── Events feed ─────────────────────────────────────────────────────────
function EventsFeed({ limit = 6 }: { limit?: number }) {
  const [events, setEvents] = useState<EventRow[]>([]);
  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const r = await api.events({ limit });
        if (!cancelled) setEvents(r.events);
      } catch {
        // ignore — events are optional on the live view
      }
    };
    load();
    const t = setInterval(load, 5000);
    return () => { cancelled = true; clearInterval(t); };
  }, [limit]);

  if (!events.length) {
    return (
      <Card title="Recent Events" flush>
        <EmptyState
          icon="inbox"
          title="No events yet"
          desc="Operator commands, alarms and state transitions will appear here as they happen."
        />
      </Card>
    );
  }
  return (
    <Card title="Recent Events" flush>
      <div>
        {events.map((e) => (
          <div key={e.id} className="ev-row" style={{ padding: "7px 14px" }}>
            <span className="ev-time">{relTime(e.ts)}</span>
            <span className="ev-dot" data-sev={e.severity} />
            <span className="ev-type">{e.type}</span>
            <span className="ev-msg">{e.message}</span>
            <span className="ev-meta">{e.meta ?? "—"}</span>
          </div>
        ))}
      </div>
    </Card>
  );
}

export function relTime(ts: number): string {
  const dt = Math.max(0, Date.now() / 1000 - ts);
  if (dt < 60) return `${Math.floor(dt)}s ago`;
  if (dt < 3600) return `${Math.floor(dt / 60)}m ago`;
  if (dt < 86400) {
    const h = Math.floor(dt / 3600);
    const m = Math.floor((dt % 3600) / 60);
    return `${h}h ${m.toString().padStart(2, "0")}m ago`;
  }
  return `${Math.floor(dt / 86400)}d ago`;
}
