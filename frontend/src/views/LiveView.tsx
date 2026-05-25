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
          <div className="page-eyebrow">Live overview · {status.site.id}</div>
          <h1 className="page-title">{status.site.name}</h1>
          <div className="page-sub">
            {status.site.ratingKw} kW · {status.site.engine} · last sync{" "}
            <span className="mono">{(status.comms.rateMs / 1000).toFixed(1)} s ago</span>
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

      <StatusHero status={status} history={history} />

      <div className="row-ats" style={{ marginTop: "var(--gap)" }}>
        <AtsCard status={status} />
        <ControlsPanel state={status.state} onCommand={setConfirmCmd} />
      </div>

      <div style={{ marginTop: "var(--gap)" }}>
        <ElectricalCard reading={reading} history={history} rateMs={status.comms.rateMs} />
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
function StatusHero({ status, history }: { status: StatusBody; history: Reading[] }) {
  const state = status.state;
  const r = status.reading;
  const loadPct = r.kw != null ? Math.min(100, Math.max(0, (r.kw / Math.max(1, status.site.ratingKw)) * 100)) : 0;

  return (
    <div className="hero" data-state={state}>
      <div className="hero-top">
        <div className="hero-meta">
          <div className="hero-meta-row">
            <span className="state-badge">
              <i className="led" />
              {STATE_BADGE[state]}
            </span>
            <span className="state-time mono">{formatTimeInState(status.timeInState)}</span>
          </div>
          <div className="state-title">{STATE_LABEL[state]}</div>
          <div className="state-sub">
            <strong>{STATE_SUB[state]}</strong>
            <span className="dot-sep" />
            <span>HTS-1 on {status.hts.transferredToGen ? "GENERATOR" : "UTILITY"}</span>
          </div>
        </div>
        <div className="hero-load">
          <LoadRing pct={loadPct} kw={r.kw} ratingKw={status.site.ratingKw} />
        </div>
      </div>
      <div className="hero-kpis">
        <HeroKpi
          icon="wave"
          label="Frequency"
          value={r.hz != null ? r.hz.toFixed(1) : "0.0"}
          unit="Hz"
          spark={history.map((h) => h.hz ?? 0).reverse()}
          color="var(--green)"
        />
        <HeroKpi
          icon="bolt"
          label="Real Power"
          value={fmt(r.kw)}
          unit="kW"
          spark={history.map((h) => h.kw ?? 0).reverse()}
          color="var(--amber)"
        />
        <HeroKpi
          icon="gauge"
          label="Engine RPM"
          value={fmt(r.rpm)}
          unit="rpm"
          spark={history.map((h) => h.rpm ?? 0).reverse()}
          color="var(--blue)"
        />
        <HeroKpi
          icon="cable"
          label="Voltage L-L"
          value={r.vAB != null ? Math.round(r.vAB).toString() : "—"}
          unit="V"
          spark={history.map((h) => h.vAB ?? 0).reverse()}
          color="var(--violet)"
        />
      </div>
    </div>
  );
}

function HeroKpi({
  icon, label, value, unit, spark, color,
}: {
  icon: any; label: string; value: string; unit: string;
  spark: number[]; color: string;
}) {
  return (
    <div className="hero-kpi">
      <div className="l">
        <span className="icn"><Icon name={icon} size={12} stroke={1.8} /></span>
        {label}
      </div>
      <div className="v">{value}<span className="u">{unit}</span></div>
      <div className="spark">
        <Sparkline points={spark} width={260} height={36} color={color} strokeWidth={1.6} />
      </div>
    </div>
  );
}

function LoadRing({ pct, kw, ratingKw }: { pct: number; kw: number | null; ratingKw: number }) {
  const r = 76;
  const C = 2 * Math.PI * r;
  const offset = C - (C * pct) / 100;
  return (
    <div className="hero-load-ring" aria-label={`Load ${Math.round(pct)} percent of ${ratingKw} kW`}>
      <svg viewBox="0 0 180 180">
        <circle cx="90" cy="90" r={r} className="track" />
        <circle
          cx="90"
          cy="90"
          r={r}
          className="head"
          style={{ strokeDasharray: C, strokeDashoffset: offset }}
        />
      </svg>
      <div className="center">
        <div className="pct">{Math.round(pct)}<span className="u">%</span></div>
        <div className="lbl">
          {fmt(kw)} / {ratingKw} kW
        </div>
      </div>
    </div>
  );
}

// ─── ATS card (separate from hero) ───────────────────────────────────────
function AtsCard({ status }: { status: StatusBody }) {
  const onGen = status.hts.transferredToGen;
  const r = status.reading;
  const loadPct = r.kw != null ? Math.round((r.kw / Math.max(1, status.site.ratingKw)) * 100) : 0;
  return (
    <Card
      title="HTS-1 Transfer Switch"
      sub={`Source: ${onGen ? "Generator" : "Utility"} · Load ${loadPct}%`}
      actions={
        <Pill tone={status.state === "alarm" ? "alarm" : onGen ? "ok" : "info"}>
          {onGen ? "Transferred" : "Normal"}
        </Pill>
      }
    >
      <div className="ats">
        <div className="ats-svg-wrap">
          <svg width="100%" height="170" viewBox="0 0 480 170" preserveAspectRatio="xMidYMid meet">
            <defs>
              <linearGradient id="ats-glow-g" x1="0" y1="0" x2="1" y2="0">
                <stop offset="0%" stopColor="var(--green)" stopOpacity="0.95" />
                <stop offset="100%" stopColor="var(--green)" stopOpacity="0.35" />
              </linearGradient>
              <linearGradient id="ats-glow-b" x1="0" y1="0" x2="1" y2="0">
                <stop offset="0%" stopColor="var(--blue)" stopOpacity="0.95" />
                <stop offset="100%" stopColor="var(--blue)" stopOpacity="0.35" />
              </linearGradient>
            </defs>

            {/* Utility block */}
            <rect x="12" y="22" width="120" height="48" rx="9"
                  fill="var(--panel)" stroke={!onGen ? "var(--blue)" : "var(--border-2)"} strokeWidth="1.5" />
            <text x="72" y="42" textAnchor="middle" fontSize="10" fontFamily="Geist" fontWeight="600" fill="var(--text-3)" letterSpacing="2">UTILITY</text>
            <text x="72" y="60" textAnchor="middle" fontSize="13" fontFamily="JetBrains Mono" fontWeight="500" fill="var(--text)">480 V</text>

            {/* Generator block */}
            <rect x="12" y="98" width="120" height="48" rx="9"
                  fill="var(--panel)" stroke={onGen ? "var(--green)" : "var(--border-2)"} strokeWidth="1.5" />
            <text x="72" y="118" textAnchor="middle" fontSize="10" fontFamily="Geist" fontWeight="600" fill="var(--text-3)" letterSpacing="2">GENERATOR</text>
            <text x="72" y="136" textAnchor="middle" fontSize="13" fontFamily="JetBrains Mono" fontWeight="500" fill="var(--text)">
              {r.vAB != null ? Math.round(r.vAB) : 0} V
            </text>

            {/* Wires */}
            <line x1="132" y1="46" x2="210" y2="46" stroke={!onGen ? "url(#ats-glow-b)" : "var(--border-2)"} strokeWidth="2" />
            <line x1="132" y1="122" x2="210" y2="122" stroke={onGen ? "url(#ats-glow-g)" : "var(--border-2)"} strokeWidth="2" />

            {/* ATS block */}
            <rect x="210" y="32" width="84" height="104" rx="11"
                  fill="var(--panel-2)" stroke="var(--border-2)" strokeWidth="1.5" />
            <text x="252" y="54" textAnchor="middle" fontSize="10" fontFamily="Geist" fontWeight="600" fill="var(--text-3)" letterSpacing="2">ATS</text>
            <text x="252" y="68" textAnchor="middle" fontSize="9" fontFamily="JetBrains Mono" fill="var(--text-4)">HTS-1</text>

            {/* Contacts */}
            <circle cx="226" cy="86" r="3.5" fill={!onGen ? "var(--blue)" : "var(--text-4)"} />
            <circle cx="226" cy="116" r="3.5" fill={onGen ? "var(--green)" : "var(--text-4)"} />
            <circle cx="278" cy="101" r="3.5" fill="var(--text-2)" />
            <line x1="226" y1={onGen ? 116 : 86} x2="278" y2="101"
                  stroke={onGen ? "var(--green)" : "var(--blue)"} strokeWidth="2.5" strokeLinecap="round" />

            {/* Load wire */}
            <line x1="294" y1="84" x2="378" y2="84"
                  stroke={onGen ? "url(#ats-glow-g)" : "url(#ats-glow-b)"} strokeWidth="2" />

            {/* Load block */}
            <rect x="378" y="60" width="90" height="48" rx="9"
                  fill="var(--panel)" stroke="var(--border-2)" strokeWidth="1.5" />
            <text x="423" y="80" textAnchor="middle" fontSize="10" fontFamily="Geist" fontWeight="600" fill="var(--text-3)" letterSpacing="2">LOAD</text>
            <text x="423" y="98" textAnchor="middle" fontSize="13" fontFamily="JetBrains Mono" fontWeight="500" fill="var(--text)">{fmt(r.kw)} kW</text>
          </svg>
        </div>

        <div className="ats-grid">
          <div className="ats-stat">
            <div className="l">Load</div>
            <div className="v">{loadPct}<span className="u">%</span></div>
          </div>
          <div className="ats-stat">
            <div className="l">Last transfer</div>
            <div className="v dim">—</div>
          </div>
          <div className="ats-stat">
            <div className="l">Transfers (30d)</div>
            <div className="v dim">—</div>
          </div>
        </div>
      </div>
    </Card>
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
    <div className="alarm-strip" role="alert" style={{ marginBottom: "var(--gap)" }}>
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
    <div className="phase-row" style={{ ["--phase-color" as any]: color }}>
      <div className="ph">{label}</div>
      <div className="bar"><i style={{ width: `${Math.max(0, Math.min(100, pct))}%` }} /></div>
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
      <div className="grid g-2" style={{ gap: 24 }}>
        <div>
          <div className="label-row" style={{ marginBottom: 10 }}>
            <span>Voltage L–L</span>
            <span className="mono" style={{ textTransform: "none", letterSpacing: 0 }}>
              {Math.round(vAvg)} V <span style={{ color: "var(--text-4)" }}>avg</span>
            </span>
          </div>
          <PhaseRow label="A–B" value={fmt(r.vAB)} unit=" V" pct={(r.vAB ?? 0) / vMax * 100} color="var(--green)" />
          <PhaseRow label="B–C" value={fmt(r.vBC)} unit=" V" pct={(r.vBC ?? 0) / vMax * 100} color="var(--amber)" />
          <PhaseRow label="C–A" value={fmt(r.vCA)} unit=" V" pct={(r.vCA ?? 0) / vMax * 100} color="var(--blue)" />
          <div style={{ marginTop: 14, opacity: 0.9 }}>
            <Sparkline points={history.map((h) => h.vAB ?? 0).reverse()} width={320} height={54} color="var(--text-3)" />
          </div>
        </div>
        <div>
          <div className="label-row" style={{ marginBottom: 10 }}>
            <span>Current per phase</span>
            <span className="mono" style={{ textTransform: "none", letterSpacing: 0 }}>
              {Math.round(iAvg)} A <span style={{ color: "var(--text-4)" }}>avg</span>
            </span>
          </div>
          <PhaseRow label="A" value={fmt(r.iA)} unit=" A" pct={(r.iA ?? 0) / iMax * 100} color="var(--green)" />
          <PhaseRow label="B" value={fmt(r.iB)} unit=" A" pct={(r.iB ?? 0) / iMax * 100} color="var(--amber)" />
          <PhaseRow label="C" value={fmt(r.iC)} unit=" A" pct={(r.iC ?? 0) / iMax * 100} color="var(--blue)" />
          <div style={{ marginTop: 14, opacity: 0.9 }}>
            <Sparkline points={history.map((h) => h.iA ?? 0).reverse()} width={320} height={54} color="var(--text-3)" />
          </div>
        </div>
      </div>

      <div className="grid g-3" style={{ gap: 16, marginTop: 22, paddingTop: 20, borderTop: "1px solid var(--border)" }}>
        <BigMetric label="Frequency" value={r.hz != null ? r.hz.toFixed(1) : "—"} unit="Hz"
                   tone={r.hz != null && r.hz > 1 ? (Math.abs(r.hz - 60) < 0.5 ? "ok" : "warn") : undefined}
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
      <div className="mono" style={{ fontSize: 34, fontWeight: 500, marginTop: 10, letterSpacing: "-0.018em" }}>
        {value}<span style={{ fontSize: 15, color: "var(--text-3)", marginLeft: 5, fontWeight: 400 }}>{unit}</span>
      </div>
      <div style={{ marginTop: 10, opacity: 0.9 }}>
        <Sparkline points={sparkPoints} width={240} height={48} color={sparkColor} />
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
      <div className="grid g-2" style={{ gap: 18 }}>
        <EngineMetric label="RPM"        value={fmt(r.rpm)}            unit="rpm" sparkPoints={history.map((h) => h.rpm ?? 0).reverse()} color="var(--green)" warnRange={[1750, 1850]} numeric={r.rpm} min={0} max={2200} />
        <EngineMetric label="Oil pres."  value={r.oilP != null ? r.oilP.toFixed(0) : "—"} unit="psi" sparkPoints={history.map((h) => h.oilP ?? 0).reverse()} color="var(--blue)"  warnRange={[35, 80]} numeric={r.oilP} min={0} max={100} />
        <EngineMetric label="Coolant"    value={r.coolT != null ? r.coolT.toFixed(0) : "—"} unit="°F" sparkPoints={history.map((h) => h.coolT ?? 0).reverse()} color="var(--amber)" warnRange={[170, 210]} numeric={r.coolT} min={50} max={250} />
        <EngineMetric label="Battery"    value={r.batt != null ? r.batt.toFixed(2) : "—"} unit="V" sparkPoints={history.map((h) => h.batt ?? 0).reverse()} color="var(--violet)" warnRange={[12.6, 14.4]} numeric={r.batt} min={10} max={16} />
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
    <div style={{ padding: "4px 6px" }}>
      <div className="label-row">
        <span>{label}</span>
        {warnRange && (
          <span style={{ textTransform: "none", letterSpacing: 0, color: inBand ? "var(--text-4)" : "var(--amber)", fontWeight: 500 }} className="mono">
            {warnRange[0]}–{warnRange[1]}
          </span>
        )}
      </div>
      <div className="mono" style={{ fontSize: 30, fontWeight: 500, marginTop: 8, letterSpacing: "-0.018em",
                                     color: inBand ? "var(--text)" : "var(--amber)" }}>
        {value}<span style={{ fontSize: 14, color: "var(--text-3)", marginLeft: 4, fontWeight: 400 }}>{unit}</span>
      </div>
      <div style={{ marginTop: 8 }}>
        <Sparkline points={sparkPoints} width={220} height={44} color={color} />
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
          <span className="icon"><Icon name="play" size={18} /></span>
          <span><div className="lbl">Remote Start</div><div className="desc">Crank engine, transfer to gen</div></span>
          <span className="kbd">⌘S</span>
        </button>
        <button className="ctl-btn" data-tone="stop" disabled={!canStop} onClick={() => onCommand("stop")}>
          <span className="icon"><Icon name="stop" size={16} /></span>
          <span><div className="lbl">Remote Stop</div><div className="desc">Cool-down then engine-off</div></span>
          <span className="kbd">⌘.</span>
        </button>
        <button className="ctl-btn" data-tone="exer" disabled={!canExercise} onClick={() => onCommand("exercise")}>
          <span className="icon"><Icon name="activity" size={18} /></span>
          <span><div className="lbl">Quiet-Test</div><div className="desc">Run unloaded · 30 min default</div></span>
          <span className="kbd">⌘E</span>
        </button>
        <button className="ctl-btn" data-tone="xfer" disabled={!canTransfer} onClick={() => onCommand("transfer")}>
          <span className="icon"><Icon name="switch_" size={20} /></span>
          <span><div className="lbl">Transfer back</div><div className="desc">HTS-1 → Utility, cool engine</div></span>
          <span className="kbd">⌘T</span>
        </button>
      </div>
      <div style={{ marginTop: 16, padding: "12px 14px", background: "var(--panel-2)", borderRadius: 10,
                    border: "1px solid var(--border)", fontSize: 12, color: "var(--text-3)",
                    display: "flex", gap: 12, alignItems: "flex-start", fontWeight: 500 }}>
        <Icon name="lock" size={14} />
        <div>Commands write to <span className="mono" style={{ color: "var(--text-2)" }}>0x019C / 0x022B / 0x012E</span> via FC16.
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
      <div style={{ padding: "4px 0 16px" }}>
        <div className="label-row" style={{ padding: "0 0 10px" }}>
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
        <div className="flex jc-sb mono" style={{ marginTop: 8, fontSize: 10.5, color: "var(--text-4)", fontWeight: 500 }}>
          <span>0</span><span>25</span><span>50</span><span>75</span><span>100 %</span>
        </div>
      </div>

      <div className="kv" style={{ marginTop: 10, paddingTop: 14, borderTop: "1px solid var(--border)" }}>
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
          <div key={e.id} className="ev-row" data-sev={e.severity}>
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
