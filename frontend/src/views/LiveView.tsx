// Live dashboard: hero, electrical, controls, engine, fuel, events.

import { useEffect, useState } from "react";
import { api } from "../api/client";
import { Card, EmptyState, Icon, LiveTick, Pill, Sparkline, fmt, formatTimeInState } from "../components/primitives";
import type { ActiveAlarm, AtsBlock, AtsMode, EngineState, EventRow, LoadSource, Reading, Role, StatusBody } from "../types";
import { ConfirmModal, type ConfirmCmd } from "./ConfirmModal";

interface Props {
  status: StatusBody;
  history: Reading[];
  operator: string;
  // Operator's role — gates admin-only ATS commands (force-transfer) in
  // the UI. The backend enforces the same gate server-side regardless.
  role: Role;
  // True when the live link is stale: WebSocket down OR no push received
  // within ~3 prime intervals. Plumbed in from App so ControlsPanel can
  // block remote commands — issuing Stop based on a frozen "running"
  // reading after the engine has actually quit would be a real-world
  // safety regression. The same flag drives the red STALE DATA badge
  // in the topbar so the two signals stay in sync.
  stale: boolean;
  // Panel-specific staleness: true when we haven't received a snapshot
  // *containing the panel block* recently, even if other messages keep
  // flowing. Distinct from `stale` so a backend that drops `panel`
  // from snapshots can't leave the panel-mode gate running against
  // a frozen seed while the rest of the UI looks live.
  panelStale: boolean;
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
// Sub-line under the state title. For `running` the meaning depends on
// whether the ATS has actually transferred load — running-unloaded is
// the warm-up window before transfer (or a forced test); running-with-
// load is the backup-power case. stateSubFor() picks the right one.
const STATE_SUB: Record<EngineState, string> = {
  stopped: "AUTO · Ready",
  cranking: "Engine start in progress",
  running: "On load · Backup power",       // overridden when loadSource = utility
  exercising: "Quiet-Test · No load",
  cooling: "Engine cool-down",
  alarm: "Shutdown · Operator action required",
  unknown: "—",
};
function stateSubFor(state: EngineState, loadSource: LoadSource | undefined): string {
  if (loadSource === "transferring") {
    return "ATS transferring · brief load gap";
  }
  if (state === "running" && loadSource === "utility") {
    return "Running unloaded · Pre-transfer warm-up";
  }
  if (state === "cooling") {
    return "Engine cool-down · Load on utility";
  }
  return STATE_SUB[state];
}

function loadSourceLabel(ls: LoadSource): string {
  if (ls === "generator") return "GENERATOR";
  if (ls === "utility") return "UTILITY";
  if (ls === "transferring") return "TRANSFERRING…";
  return "—";
}
const STATE_BADGE: Record<EngineState, string> = {
  stopped: "STOPPED",
  cranking: "CRANKING",
  running: "ON LOAD",
  exercising: "EXERCISING",
  cooling: "COOLING",
  alarm: "ALARM",
  unknown: "—",
};

export function LiveView({ status, history, operator, role, stale, panelStale }: Props) {
  const [confirmCmd, setConfirmCmd] = useState<ConfirmCmd | null>(null);

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
        <AtsCard status={status} role={role} onCommand={setConfirmCmd} stale={stale} />
        <ControlsPanel
          state={status.state}
          panelMode={status.panel.mode}
          stale={stale}
          panelStale={panelStale}
          onCommand={(v) => setConfirmCmd({ kind: v })}
        />
      </div>

      <div style={{ marginTop: "var(--gap)" }}>
        <ElectricalCard reading={reading} history={history} rateMs={status.comms.rateMs} />
      </div>

      <div className="row" style={{ marginTop: "var(--gap)" }}>
        <EngineCard status={status} history={history} />
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
            <strong>{stateSubFor(state, status.loadSource)}</strong>
            <span className="dot-sep" />
            <span>HTS-1 on {loadSourceLabel(status.loadSource)}</span>
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
          value={r.hz != null ? r.hz.toFixed(1) : "—"}
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
function AtsCard({ status, role, onCommand, stale }: {
  status: StatusBody;
  role: Role;
  onCommand: (cmd: ConfirmCmd) => void;
  stale: boolean;
}) {
  // Drive the diagram from the load-source classifier rather than the
  // legacy boolean — this keeps the visualization accurate during
  // quiet-test exercises (engine running, load still on utility) and
  // during the pre-transfer warm-up window.
  const onGen = status.loadSource === "generator";
  const transferring = status.loadSource === "transferring";
  const r = status.reading;
  const loadPct = r.kw != null ? Math.round((r.kw / Math.max(1, status.site.ratingKw)) * 100) : 0;

  // ATS-Pi block — when present, surface its richer signals. When
  // ats.enabled is false, the card behaves exactly as before (driven
  // off the H-100-derived loadSource).
  const ats = status.ats.enabled ? status.ats : null;

  // Last-transfer source preference: prefer ATS-Pi's direct observation
  // when available; fall back to the H-100-derived event-log estimate.
  const lastTransferTs =
    ats?.lastTransferToGenTs ?? status.hts.lastTransferTs ?? null;
  const transfers24h = ats?.transferCount24h;

  // Status pill (top-right): describes what the switch is doing.
  const pill = transferring
    ? { tone: "info" as const, label: "Transferring…" }
    : onGen
    ? { tone: "ok" as const, label: "Transferred" }
    : { tone: "info" as const, label: "Normal" };

  // Provenance pill — shown only when an ATS-Pi is configured.
  // Operators care which path the displayed source comes from when
  // making decisions during commissioning or a fault.
  const provenance = ats
    ? ats.authoritative
      ? { tone: "ok" as const, label: "via ATS-Pi" }
      : ats.comms.state === "healthy"
      ? { tone: "warn" as const, label: "ATS-Pi waiting" }
      : { tone: "warn" as const, label: "ATS-Pi link down · using gen telemetry" }
    : null;

  return (
    <Card
      title="HTS-1 Transfer Switch"
      sub={`Source: ${transferring ? "Transferring…" : onGen ? "Generator" : "Utility"} · Load ${loadPct}%`}
      actions={
        <div className="flex ai-c gap-8">
          {provenance && <Pill tone={provenance.tone}>{provenance.label}</Pill>}
          <Pill tone={status.state === "alarm" ? "alarm" : pill.tone}>{pill.label}</Pill>
        </div>
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

            {/* Utility block — colour reflects source availability when
                the ATS-Pi reports it: amber when reportedly unavailable. */}
            <rect x="12" y="22" width="120" height="48" rx="9"
                  fill="var(--panel)"
                  stroke={
                    ats && ats.normalAvailable === false ? "var(--amber)"
                      : !onGen ? "var(--blue)"
                      : "var(--border-2)"
                  }
                  strokeWidth="1.5" />
            <text x="72" y="42" textAnchor="middle" fontSize="10" fontFamily="Geist" fontWeight="600" fill="var(--text-3)" letterSpacing="2">UTILITY</text>
            <text x="72" y="60" textAnchor="middle" fontSize="13" fontFamily="JetBrains Mono" fontWeight="500" fill="var(--text)">
              {ats && ats.normalAvailable === false ? "LOST" : "480 V"}
            </text>

            {/* Generator block */}
            <rect x="12" y="98" width="120" height="48" rx="9"
                  fill="var(--panel)"
                  stroke={
                    ats && ats.emergencyAvailable === false ? "var(--amber)"
                      : onGen ? "var(--green)"
                      : "var(--border-2)"
                  }
                  strokeWidth="1.5" />
            <text x="72" y="118" textAnchor="middle" fontSize="10" fontFamily="Geist" fontWeight="600" fill="var(--text-3)" letterSpacing="2">GENERATOR</text>
            <text x="72" y="136" textAnchor="middle" fontSize="13" fontFamily="JetBrains Mono" fontWeight="500" fill="var(--text)">
              {ats && ats.emergencyAvailable === false ? "NOT READY"
                : r.vAB != null ? `${Math.round(r.vAB)} V` : "—"}
            </text>

            {/* Wires — animated dashed during transferring */}
            <line x1="132" y1="46" x2="210" y2="46"
                  stroke={!onGen && !transferring ? "url(#ats-glow-b)" : "var(--border-2)"}
                  strokeWidth="2"
                  strokeDasharray={transferring ? "5 3" : undefined}
                  opacity={transferring ? 0.6 : 1}>
              {transferring && (
                <animate attributeName="stroke-dashoffset" from="0" to="-16" dur="0.6s" repeatCount="indefinite" />
              )}
            </line>
            <line x1="132" y1="122" x2="210" y2="122"
                  stroke={onGen && !transferring ? "url(#ats-glow-g)" : "var(--border-2)"}
                  strokeWidth="2"
                  strokeDasharray={transferring ? "5 3" : undefined}
                  opacity={transferring ? 0.6 : 1}>
              {transferring && (
                <animate attributeName="stroke-dashoffset" from="0" to="-16" dur="0.6s" repeatCount="indefinite" />
              )}
            </line>

            {/* ATS block */}
            <rect x="210" y="32" width="84" height="104" rx="11"
                  fill="var(--panel-2)" stroke="var(--border-2)" strokeWidth="1.5" />
            <text x="252" y="54" textAnchor="middle" fontSize="10" fontFamily="Geist" fontWeight="600" fill="var(--text-3)" letterSpacing="2">ATS</text>
            <text x="252" y="68" textAnchor="middle" fontSize="9" fontFamily="JetBrains Mono" fill="var(--text-4)">HTS-1</text>

            {/* Contacts — mid-position during transferring */}
            <circle cx="226" cy="86" r="3.5"
                    fill={!onGen && !transferring ? "var(--blue)" : "var(--text-4)"} />
            <circle cx="226" cy="116" r="3.5"
                    fill={onGen && !transferring ? "var(--green)" : "var(--text-4)"} />
            <circle cx="278" cy="101" r="3.5" fill="var(--text-2)" />
            {!transferring && (
              <line x1="226" y1={onGen ? 116 : 86} x2="278" y2="101"
                    stroke={onGen ? "var(--green)" : "var(--blue)"}
                    strokeWidth="2.5" strokeLinecap="round" />
            )}
            {transferring && (
              <line x1="226" y1={101} x2="278" y2="101"
                    stroke="var(--amber)" strokeWidth="2.5" strokeLinecap="round"
                    strokeDasharray="4 3">
                <animate attributeName="stroke-dashoffset" from="0" to="-14" dur="0.4s" repeatCount="indefinite" />
              </line>
            )}

            {/* Load wire */}
            <line x1="294" y1="84" x2="378" y2="84"
                  stroke={transferring ? "var(--amber)"
                    : onGen ? "url(#ats-glow-g)"
                    : "url(#ats-glow-b)"}
                  strokeWidth="2"
                  opacity={transferring ? 0.5 : 1} />

            {/* Load block */}
            <rect x="378" y="60" width="90" height="48" rx="9"
                  fill="var(--panel)" stroke="var(--border-2)" strokeWidth="1.5" />
            <text x="423" y="80" textAnchor="middle" fontSize="10" fontFamily="Geist" fontWeight="600" fill="var(--text-3)" letterSpacing="2">LOAD</text>
            <text x="423" y="98" textAnchor="middle" fontSize="13" fontFamily="JetBrains Mono" fontWeight="500" fill="var(--text)">{fmt(r.kw)} kW</text>
          </svg>
        </div>

        {/* Source-availability + mode chips, only when the ATS-Pi is
            present. These are the signals you can't derive from the
            H-100 alone — utility-side health, manual lockout, etc. */}
        {ats && (
          <div className="ats-chips" style={{
            display: "flex", gap: 10, padding: "8px 4px 12px", flexWrap: "wrap",
          }}>
            <SourceChip label="Normal" available={ats.normalAvailable} />
            <SourceChip label="Emergency" available={ats.emergencyAvailable} />
            <ModeChip mode={ats.atsMode} />
            {ats.engineStartCalling && (
              <Pill tone="info">ATS calling engine start</Pill>
            )}
            {ats.faultCodes.map((c) => (
              <Pill key={c} tone="warn">{c.replace(/^ATS_PI_/, "")}</Pill>
            ))}
          </div>
        )}

        {/* ATS command row (Phase 3, ICD §6). Two-step confirm-token
            gated; disabled unless the ATS-Pi link is authoritative.
            Force Transfer is admin-only (also enforced server-side). */}
        {ats && <AtsControls ats={ats} role={role} stale={stale} onCommand={onCommand} />}

        <div className="ats-grid">
          <div className="ats-stat">
            <div className="l">Load</div>
            <div className="v">{loadPct}<span className="u">%</span></div>
          </div>
          <div className="ats-stat">
            <div className="l">Last transfer</div>
            <div className="v dim">
              {lastTransferTs != null ? relTime(lastTransferTs) : "—"}
            </div>
          </div>
          <div className="ats-stat">
            <div className="l">{transfers24h !== undefined ? "Transfers (24h)" : "Transfers (30d)"}</div>
            <div className="v">{transfers24h ?? status.hts.transfers30d}</div>
          </div>
        </div>
      </div>
    </Card>
  );
}

function AtsControls({ ats, role, stale, onCommand }: {
  ats: Extract<AtsBlock, { enabled: true }>;
  role: Role;
  stale: boolean;
  onCommand: (cmd: ConfirmCmd) => void;
}) {
  // Gate on the ATS link being authoritative (comms healthy + ICD/unit
  // match) and the overall UI not being stale (a dead WS freezes the
  // snapshot these flags come from). Mirrors the backend, which returns
  // 502/409 when not authoritative.
  const linkOk = !stale && ats.authoritative && ats.comms.state !== "lost";
  const isAdmin = role === "admin";
  const inhibitOn = ats.cmdInhibitActive;
  const forceOn = ats.cmdForceTransferActive;
  const linkHint = !linkOk
    ? "ATS-Pi link is not authoritative — commands are disabled until it recovers."
    : undefined;
  const btn = (active: boolean) =>
    `btn ${active ? "btn-danger" : "btn-ghost"}`;
  const sz = { fontSize: 12, padding: "6px 11px" } as const;
  return (
    <div className="ats-controls" style={{ display: "flex", gap: 8, flexWrap: "wrap", padding: "2px 4px 12px" }}>
      <button className="btn btn-ghost" style={sz} disabled={!linkOk} title={linkHint}
              onClick={() => onCommand({ kind: "ats_test" })}>
        Test
      </button>
      <button className={btn(inhibitOn)} style={sz} disabled={!linkOk} title={linkHint}
              onClick={() => onCommand({ kind: "ats_inhibit", assert: !inhibitOn })}>
        {inhibitOn ? "Release Inhibit" : "Inhibit"}
      </button>
      <button className={btn(forceOn)} style={sz}
              disabled={!linkOk || !isAdmin}
              title={!isAdmin ? "Force Transfer requires the admin role." : linkHint}
              onClick={() => onCommand({
                kind: "ats_force_transfer",
                assert: !forceOn,
                // Override is needed only when ASSERTING while utility is
                // still available; the modal surfaces the warning copy.
                override: !forceOn && ats.normalAvailable === true,
              })}>
        {forceOn ? "Release Force" : "Force Transfer"}
      </button>
      <button className="btn btn-ghost" style={sz} disabled={!linkOk} title={linkHint}
              onClick={() => onCommand({ kind: "ats_bypass_delay" })}>
        Bypass Delay
      </button>
    </div>
  );
}

function SourceChip({ label, available }: { label: string; available: boolean | null | undefined }) {
  if (available === null || available === undefined) {
    return <Pill tone="info">{label}: —</Pill>;
  }
  return (
    <Pill tone={available ? "ok" : "warn"}>
      {label}: {available ? "available" : "lost"}
    </Pill>
  );
}

function ModeChip({ mode }: { mode: AtsMode }) {
  if (mode === "auto") return <Pill tone="ok">ATS · AUTO</Pill>;
  if (mode === "manual") return <Pill tone="warn">ATS · MANUAL</Pill>;
  if (mode === "test") return <Pill tone="info">ATS · TEST</Pill>;
  return <Pill tone="info">ATS · mode unknown</Pill>;
}

// ─── Alarm strip ──────────────────────────────────────────────────────────
function AlarmStrip({ alarm }: { alarm: ActiveAlarm }) {
  // `submitting` is true ONLY while the ack request is in flight — we no
  // longer optimistically flip to "Acknowledged" before the controller
  // has actually been written. On success the row disappears when the
  // alarm-cleared WS message / next snapshot drops it from activeAlarms;
  // until then the operator still sees the alarm (correct — it's still
  // latched). Failures (e.g. 502 "Modbus ack write failed", or a
  // local-only clear with hw_ack=false) surface inline so the operator
  // never believes a still-latched alarm was cleared.
  const [submitting, setSubmitting] = useState(false);
  const [ackError, setAckError] = useState<string | null>(null);
  const onAck = async () => {
    setSubmitting(true);
    setAckError(null);
    try {
      const res = await api.ackAlarm(alarm.code);
      if (!res.hw_ack) {
        setAckError(
          "Cleared locally, but the controller was not written (no ack control mapped) — "
          + "the panel may still be latched; acknowledge at the H-100."
        );
      }
    } catch (e: any) {
      const detail = e?.body?.detail;
      const msg =
        detail?.message ??
        (typeof detail === "string" ? detail : null) ??
        e?.message ??
        "Acknowledge failed — the controller may still be latched.";
      setAckError(msg);
    } finally {
      setSubmitting(false);
    }
  };
  const ago = Math.floor((Date.now() / 1000 - alarm.raised_at));
  const agoStr = `${Math.floor(ago / 60)}m ${ago % 60}s`;
  return (
    <div className="alarm-strip" role="alert" style={{ marginBottom: "var(--gap)", flexWrap: "wrap" }}>
      <span className="led" />
      <strong>Active alarm</strong>
      <span>{alarm.desc} · raised {agoStr} ago</span>
      <button className="btn btn-danger" disabled={submitting} onClick={onAck}>
        {submitting ? "Acknowledging…" : "Acknowledge"}
      </button>
      {ackError && (
        <span style={{ flexBasis: "100%", marginTop: 6, color: "var(--red)", fontSize: 12.5, fontWeight: 600 }}>
          {ackError}
        </span>
      )}
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

      <div className="grid g-4" style={{ gap: 16, marginTop: 22, paddingTop: 20, borderTop: "1px solid var(--border)" }}>
        <BigMetric label="Frequency" value={r.hz != null ? r.hz.toFixed(1) : "—"} unit="Hz"
                   tone={r.hz != null && r.hz > 1 ? (Math.abs(r.hz - 60) < 0.5 ? "ok" : "warn") : undefined}
                   sparkPoints={history.map((h) => h.hz ?? 0).reverse()} sparkColor="var(--green)" />
        <BigMetric label="Real power" value={fmt(r.kw)} unit="kW"
                   sparkPoints={history.map((h) => h.kw ?? 0).reverse()} sparkColor="var(--amber)" />
        <BigMetric label="Apparent" value={fmt(r.kw != null ? Math.round(r.kw * 1.07) : null)} unit="kVA"
                   sparkPoints={history.map((h) => (h.kw ?? 0) * 1.07).reverse()} sparkColor="var(--blue)" />
        <BigMetric label="Power factor"
                   value={r.pf != null ? r.pf.toFixed(2) : "—"} unit="pf"
                   sparkPoints={history.map((h) => h.pf ?? 0).reverse()} sparkColor="var(--violet)" />
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
function EngineCard({ status, history }: { status: StatusBody; history: Reading[] }) {
  const r = status.reading;
  // Battery range covers both 12V and 24V systems — the H-100 is wired
  // for a 24V starting bank on most installs (~26 V float), but smaller
  // configurations exist. We don't hard-code which; the warn range is
  // a permissive band covering both float-charged states.
  const battWarn: [number, number] = (r.batt ?? 0) > 18 ? [25.0, 29.5] : [12.6, 14.4];

  // Hide the O₂ sensor entirely on diesel sites. Diesels have no O₂
  // probe — the H-100 register reads 0 always, and showing a constant-
  // zero gauge is just noise. Gated on site.fuelType which the operator
  // sets in registers/h100.yaml.
  const isDiesel = status.site.fuelType === "diesel";

  // Throttle annotation: when the engine is running unloaded (warm-up
  // or quiet-test), 0% is the correct reading, not a sensor fault.
  // Label it so an operator scanning the panel can tell the difference
  // between "no demand" and "missing data".
  const isRunningClass = status.state === "running" || status.state === "exercising";
  const throttleNote =
    isRunningClass && status.loadSource !== "generator"
      ? (status.state === "exercising" ? "quiet test" : "no load")
      : undefined;

  return (
    <Card title="Engine" sub={status.site.engine}
          actions={<Pill tone={r.oilP != null && r.oilP < 25 && (r.rpm ?? 0) > 100 ? "alarm" : "ok"}>nominal</Pill>}>
      <div className="grid g-4" style={{ gap: 18 }}>
        <EngineMetric label="RPM"           value={fmt(r.rpm)}            unit="rpm" sparkPoints={history.map((h) => h.rpm ?? 0).reverse()} color="var(--green)"  warnRange={[1750, 1850]} numeric={r.rpm}      min={0}  max={2200} />
        <EngineMetric label="Oil pres."     value={r.oilP != null ? r.oilP.toFixed(0) : "—"} unit="psi" sparkPoints={history.map((h) => h.oilP ?? 0).reverse()} color="var(--blue)"   warnRange={[35, 80]}     numeric={r.oilP}     min={0}  max={100} />
        <EngineMetric label="Oil temp"      value={r.oilT != null ? r.oilT.toFixed(0) : "—"} unit="°F"  sparkPoints={history.map((h) => h.oilT ?? 0).reverse()} color="var(--amber)"  warnRange={[160, 250]}   numeric={r.oilT}     min={0}  max={300} />
        <EngineMetric label="Coolant temp"  value={r.coolT != null ? r.coolT.toFixed(0) : "—"} unit="°F" sparkPoints={history.map((h) => h.coolT ?? 0).reverse()} color="var(--amber)" warnRange={[170, 210]}   numeric={r.coolT}    min={50} max={250} />
        <EngineMetric label="Battery"       value={r.batt != null ? r.batt.toFixed(2) : "—"} unit="V"   sparkPoints={history.map((h) => h.batt ?? 0).reverse()} color="var(--violet)" warnRange={battWarn}     numeric={r.batt}     min={10} max={32} />
        <EngineMetric label="Charge curr."  value={r.battA != null ? r.battA.toFixed(1) : "—"} unit="A" sparkPoints={history.map((h) => h.battA ?? 0).reverse()} color="var(--green)" warnRange={[0, 25]}      numeric={r.battA}    min={-5} max={40} />
        <EngineMetric label="Throttle"      value={r.throttle != null ? r.throttle.toFixed(0) : "—"} unit="%" sparkPoints={history.map((h) => h.throttle ?? 0).reverse()} color="var(--blue)" warnRange={[0, 100]} numeric={r.throttle} min={0}  max={100} note={throttleNote} />
        {!isDiesel && (
          <EngineMetric label="O₂ sensor" value={r.o2 != null ? r.o2.toFixed(0) : "—"} unit="%"     sparkPoints={history.map((h) => h.o2 ?? 0).reverse()}      color="var(--text-2)" warnRange={[0, 100]}     numeric={r.o2}       min={0}  max={100} />
        )}
      </div>
    </Card>
  );
}

function EngineMetric({ label, value, unit, sparkPoints, color, warnRange, numeric, note }: {
  label: string; value: string; unit: string;
  sparkPoints: number[]; color: string; warnRange?: [number, number];
  numeric: number | null; min: number; max: number;
  // Optional context string rendered alongside the value (e.g. "no load"
  // on throttle when running unloaded). Render dim so it doesn't compete
  // with the primary reading.
  note?: string;
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
      <div className="mono" style={{ fontSize: 24, fontWeight: 500, marginTop: 8, letterSpacing: "-0.018em",
                                     color: inBand ? "var(--text)" : "var(--amber)" }}>
        {value}<span style={{ fontSize: 12, color: "var(--text-3)", marginLeft: 3, fontWeight: 400 }}>{unit}</span>
        {note && (
          <span style={{ fontSize: 11, color: "var(--text-4)", marginLeft: 8, fontWeight: 400, letterSpacing: 0,
                         textTransform: "uppercase" }}>{note}</span>
        )}
      </div>
      <div style={{ marginTop: 8 }}>
        <Sparkline points={sparkPoints} width={170} height={36} color={color} />
      </div>
    </div>
  );
}

// ─── Controls panel ──────────────────────────────────────────────────────
// Engine states from which Remote Stop is valid. Keep in lockstep with
// backend ALLOWED["stop"] in services/control.py.
const STOPPABLE_STATES = new Set<EngineState>([
  "running", "exercising", "cranking", "cooling", "alarm",
]);

function ControlsPanel({ state, panelMode, stale, panelStale, onCommand }: {
  state: EngineState;
  panelMode: "auto" | "manual" | "off" | "unknown";
  stale: boolean;
  panelStale: boolean;
  onCommand: (cmd: "start" | "stop" | "exercise" | "transfer") => void;
}) {
  // Three gates, all required before any remote command is offered:
  //
  // 1. Live-link freshness (`linkOk`). When the WebSocket is down or the
  //    server has gone silent for ~3 prime intervals (same signal as the
  //    red STALE DATA badge in the topbar), the engine state shown here
  //    is no longer trustworthy. Issuing Stop based on a frozen
  //    "running" reading after the engine has actually quit would be a
  //    real-world safety regression — the backend's state-validity
  //    guard is the backstop, but we should never present an enabled
  //    button whose precondition we can't currently verify.
  //
  // 2. Panel key switch (`panelOk`). The H-100 only honors remote
  //    start/stop/exercise/transfer writes when the front-panel key
  //    switch is in AUTO. MANUAL/OFF locally locks out the controller's
  //    remote-command path, so an enabled UI button on a non-AUTO
  //    panel would just produce a silent no-op at the unit. "unknown"
  //    means the prime poll hasn't decoded input_status_1 yet (cold
  //    start) or the operator's panel_mode_bits YAML rules don't match
  //    their firmware — safer to gate.
  //
  // 3. Panel-block freshness (`!panelStale`). Defense-in-depth against
  //    a backend that keeps the WS alive but stops including `panel`
  //    in snapshots — without this gate the panel-mode check would run
  //    against a frozen seed value forever. With a current backend
  //    (which always emits `panel`), panelStale is only ever true
  //    when the WS itself is also stale, so this is a no-op in normal
  //    operation; the value comes from the asymmetric failure mode.
  const linkOk = !stale;
  const panelOk = panelMode === "auto" && !panelStale;
  const canStart = linkOk && panelOk && state === "stopped";
  // Mirror backend ALLOWED["stop"] (services/control.py) exactly — it
  // permits stop from cooling and alarm too. A narrower client set left
  // the operator with no remote Stop during an alarm shutdown or
  // cool-down, even though the controller would accept it.
  const canStop = linkOk && panelOk && STOPPABLE_STATES.has(state);
  const canExercise = linkOk && panelOk && state === "stopped";
  const canTransfer = linkOk && panelOk && state === "running";

  // Stale takes precedence in the operator-facing hint — it's the more
  // urgent thing to know. Panel mode is something the operator can fix
  // at the unit; a stale link means we may not even be talking to the
  // unit right now and the next push could land any second.
  const staleHint = stale
    ? "Live link is stale — values shown may be out of date. Commands are blocked until the link recovers."
    : null;
  const panelStaleHint = panelStale && !stale
    ? "Panel state has not refreshed recently — backend may not be emitting panel data. Commands are blocked until a fresh snapshot arrives."
    : null;
  const panelHint =
    panelMode === "auto"           ? null :
    panelMode === "manual"         ? "Panel key switch is MANUAL — set to AUTO at the unit to enable remote commands." :
    panelMode === "off"            ? "Panel key switch is OFF — engine is locked out. Set to AUTO at the unit." :
    /* unknown */                    "Panel key-switch position is unknown — waiting for prime poll or verify panel_mode_bits in h100.yaml.";
  const hint = staleHint ?? panelStaleHint ?? panelHint;

  return (
    <Card title="Controls" sub="Operator · two-step confirm">
      <div className="ctl-stack">
        <button className="ctl-btn" data-tone="start" disabled={!canStart} onClick={() => onCommand("start")}
                title={hint ?? undefined}>
          <span className="icon"><Icon name="play" size={18} /></span>
          <span><div className="lbl">Remote Start</div><div className="desc">Crank engine · load stays on utility</div></span>
          <span className="kbd">⌘S</span>
        </button>
        <button className="ctl-btn" data-tone="stop" disabled={!canStop} onClick={() => onCommand("stop")}
                title={hint ?? undefined}>
          <span className="icon"><Icon name="stop" size={16} /></span>
          <span><div className="lbl">Remote Stop</div><div className="desc">Retransfer to utility · cool-down · stop</div></span>
          <span className="kbd">⌘.</span>
        </button>
        <button className="ctl-btn" data-tone="exer" disabled={!canExercise} onClick={() => onCommand("exercise")}
                title={hint ?? undefined}>
          <span className="icon"><Icon name="activity" size={18} /></span>
          <span><div className="lbl">Quiet-Test</div><div className="desc">Run unloaded · 30 min default</div></span>
          <span className="kbd">⌘E</span>
        </button>
        <button className="ctl-btn" data-tone="xfer" disabled={!canTransfer} onClick={() => onCommand("transfer")}
                title={hint ?? undefined}>
          <span className="icon"><Icon name="switch_" size={20} /></span>
          <span><div className="lbl">Transfer to Gen</div><div className="desc">HTS-1 → Generator (move load)</div></span>
          <span className="kbd">⌘T</span>
        </button>
      </div>
      {hint && (
        <div style={{ marginTop: 14, padding: "10px 14px",
                      background: stale
                        ? "color-mix(in oklch, var(--red) 12%, var(--panel-2))"
                        : "color-mix(in oklch, var(--amber) 12%, var(--panel-2))",
                      borderRadius: 10,
                      border: stale
                        ? "1px solid color-mix(in oklch, var(--red) 30%, var(--border))"
                        : "1px solid color-mix(in oklch, var(--amber) 30%, var(--border))",
                      fontSize: 12, color: "var(--text-2)", display: "flex", gap: 10, alignItems: "flex-start",
                      fontWeight: 500 }}>
          <Icon name={stale ? "wifi-off" : "lock"} size={14} />
          <div>{hint}</div>
        </div>
      )}
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

// ─── Fluid levels + maintenance card ─────────────────────────────────────
function FuelMaintCard({ reading: r, status }: { reading: Reading; status: StatusBody }) {
  const fuel = r.fuelPct ?? 0;
  const lowFuel = fuel < 25;
  const gal = Math.round(fuel * (status.site.tankGal / 100));
  const coolLevel = r.coolLevel;
  const lowCool = coolLevel != null && coolLevel < 50;
  // Sub-label adapts to fuel type — diesel sites have a local tank we
  // can quantify in gallons; gaseous sites typically have a utility
  // gas connection where the "tank" volume is less meaningful, but we
  // keep the field display in case the operator wants to track a local
  // LP tank.
  const fuelLabel =
    status.site.fuelType === "diesel"  ? "Local diesel" :
    status.site.fuelType === "gaseous" ? "Gaseous fuel" :
    "Tank";
  return (
    <Card title="Tank · Maintenance" sub={`${fuelLabel} · ${status.site.tankGal} gal`}>
      <div style={{ padding: "4px 0 14px" }}>
        <div className="label-row" style={{ padding: "0 0 8px" }}>
          <span>Coolant level</span>
          <span className="mono" style={{ textTransform: "none", letterSpacing: 0, color: lowCool ? "var(--amber)" : "var(--text-2)" }}>
            {coolLevel != null ? `${coolLevel.toFixed(1)} %` : "—"}
          </span>
        </div>
        <div className="fuel-bar">
          <i style={{ width: `${coolLevel ?? 0}%`, background: lowCool ? "linear-gradient(90deg, var(--amber), var(--red))" : "linear-gradient(90deg, var(--blue), color-mix(in oklch, var(--blue) 70%, var(--green)))" }} />
          <div className="ticks">
            {Array.from({ length: 9 }).map((_, i) => <i key={i} />)}
          </div>
        </div>
      </div>

      <div style={{ padding: "4px 0 16px" }}>
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
          <span className="v" title={status.lastAlarm?.message}>
            {status.activeAlarms[0]
              ? status.activeAlarms[0].desc
              : status.lastAlarm
                ? `${relTime(status.lastAlarm.ts)} — ${status.lastAlarm.message.replace(/^(Alarm raised — |Alarm cleared — |Alarm acknowledged — )/, "")}`
                : "—"}
          </span></div>
      </div>
    </Card>
  );
}

// ─── Events feed ─────────────────────────────────────────────────────────
function EventsFeed({ limit = 6 }: { limit?: number }) {
  const [events, setEvents] = useState<EventRow[]>([]);
  // Track the last fetch's success/failure so a broken /api/events
  // endpoint doesn't look the same as "no events." Previously this
  // caught + ignored the error, leaving the operator with a clean
  // empty-state card that misrepresented the real situation.
  const [fetchError, setFetchError] = useState<string | null>(null);
  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const r = await api.events({ limit });
        if (cancelled) return;
        setEvents(r.events);
        setFetchError(null);
      } catch (err: any) {
        if (cancelled) return;
        setFetchError(err?.message ?? "Failed to load events");
      }
    };
    load();
    const t = setInterval(load, 5000);
    return () => { cancelled = true; clearInterval(t); };
  }, [limit]);

  // Error banner above whatever content we have. We KEEP showing the
  // last-known events (if any) so the operator isn't blind during a
  // transient backend hiccup; the banner just tells them what they're
  // seeing is no longer fresh.
  const errorBanner = fetchError && (
    <div
      style={{
        margin: "0 0 10px",
        padding: "8px 12px",
        background: "color-mix(in oklch, var(--red) 12%, var(--panel-2))",
        border: "1px solid color-mix(in oklch, var(--red) 30%, var(--border))",
        borderRadius: 8,
        fontSize: 12,
        color: "var(--text-2)",
        display: "flex",
        gap: 8,
        alignItems: "center",
      }}
    >
      <Icon name="wifi-off" size={13} />
      <span>
        <strong style={{ color: "var(--red)" }}>Events feed unavailable</strong>
        {" — "}{fetchError}. Retrying every 5 s.
      </span>
    </div>
  );

  if (!events.length) {
    return (
      <Card title="Recent Events" flush>
        {errorBanner}
        <EmptyState
          icon="inbox"
          title={fetchError ? "Events temporarily unavailable" : "No events yet"}
          desc={
            fetchError
              ? "Showing nothing because nothing has been loaded yet. The page will retry automatically."
              : "Operator commands, alarms and state transitions will appear here as they happen."
          }
        />
      </Card>
    );
  }
  return (
    <Card title="Recent Events" flush>
      {errorBanner}
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
