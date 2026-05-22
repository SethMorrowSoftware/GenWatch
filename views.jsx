// All views: LiveDashboard, History, Events, Controls modal, Settings.

const fmt = (n, digits = 0) => {
  if (n == null) return "—";
  return Number(n).toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
};

// ─── Status hero ─────────────────────────────────────────────────────────────
function StatusHero({ state, reading, timeInState, lastSyncMs }) {
  const meta = STATES[state] || STATES.stopped;
  return (
    <div className="hero" data-state={state}>
      <div className="hero-left">
        <span className="state-badge">
          <i className="led" />
          {state === "running" ? "ON LOAD" : state.toUpperCase()}
        </span>
        <div className="state-title">{meta.label}</div>
        <div className="state-sub">{meta.sub} · <span className="state-time mono">{timeInState}</span></div>
        <div className="hero-stats">
          <div className="hero-stat">
            <div className="l">Frequency</div>
            <div className="v">{reading.hz ? reading.hz.toFixed(1) : "0.0"}<span className="u">Hz</span></div>
          </div>
          <div className="hero-stat">
            <div className="l">Real power</div>
            <div className="v">{fmt(reading.kw)}<span className="u">kW</span></div>
          </div>
          <div className="hero-stat">
            <div className="l">Engine RPM</div>
            <div className="v">{fmt(reading.rpm)}<span className="u">rpm</span></div>
          </div>
        </div>
      </div>
      <div className="hero-right">
        <AtsPanel state={state} reading={reading} />
      </div>
    </div>
  );
}

// ─── ATS Transfer Switch panel ──────────────────────────────────────────────
function AtsPanel({ state, reading }) {
  const onGen = state === "running" || state === "exercising";
  const utility = state !== "running"; // assume utility is lost only when on-load running
  return (
    <div style={{display:"flex", flexDirection:"column", gap:14}}>
      <div className="flex jc-sb ai-c">
        <div>
          <div style={{fontSize:11, textTransform:"uppercase", letterSpacing:"0.08em", color:"var(--text-3)"}}>HTS-1 Transfer Switch</div>
          <div style={{fontSize:13.5, color:"var(--text)", marginTop:4, fontWeight:500}}>
            Source: <span style={{color: onGen ? "var(--green)" : "var(--blue)"}}>{onGen ? "Generator" : "Utility"}</span>
          </div>
        </div>
        <Pill tone={state === "alarm" ? "alarm" : onGen ? "ok" : "info"}>
          {onGen ? "Transferred" : utility ? "Normal" : "Standby"}
        </Pill>
      </div>

      <div style={{position:"relative", padding:"4px 0"}}>
        <svg width="100%" height="120" viewBox="0 0 320 120" preserveAspectRatio="none" style={{display:"block"}}>
          {/* Utility source */}
          <rect x="6" y="14" width="80" height="36" rx="6" fill="var(--panel)" stroke={!onGen ? "var(--blue-d)" : "var(--border)"} />
          <text x="46" y="32" textAnchor="middle" fontSize="9" fontFamily="Geist" fill="var(--text-3)" letterSpacing="1">UTILITY</text>
          <text x="46" y="44" textAnchor="middle" fontSize="11" fontFamily="JetBrains Mono" fill="var(--text)">480 V</text>

          {/* Generator source */}
          <rect x="6" y="70" width="80" height="36" rx="6" fill="var(--panel)" stroke={onGen ? "var(--green-d)" : "var(--border)"} />
          <text x="46" y="88" textAnchor="middle" fontSize="9" fontFamily="Geist" fill="var(--text-3)" letterSpacing="1">GENERATOR</text>
          <text x="46" y="100" textAnchor="middle" fontSize="11" fontFamily="JetBrains Mono" fill="var(--text)">{reading.vAB ? Math.round(reading.vAB) : 0} V</text>

          {/* lines to ATS */}
          <line x1="86" y1="32" x2="140" y2="32" stroke={!onGen ? "var(--blue)" : "var(--border-2)"} strokeWidth="1.5" />
          <line x1="86" y1="88" x2="140" y2="88" stroke={onGen ? "var(--green)" : "var(--border-2)"} strokeWidth="1.5" />

          {/* ATS switch box */}
          <rect x="140" y="20" width="60" height="80" rx="6" fill="var(--panel-2)" stroke="var(--border-2)" />
          <text x="170" y="36" textAnchor="middle" fontSize="9" fontFamily="Geist" fill="var(--text-3)" letterSpacing="1">ATS</text>
          {/* switch contacts */}
          <circle cx="155" cy="60" r="2.5" fill={!onGen ? "var(--blue)" : "var(--text-4)"} />
          <circle cx="155" cy="78" r="2.5" fill={onGen ? "var(--green)" : "var(--text-4)"} />
          <circle cx="185" cy="60" r="2.5" fill="var(--text-2)" />
          {/* contact line */}
          <line x1="155" y1={onGen ? 78 : 60} x2="185" y2="60" stroke={onGen ? "var(--green)" : "var(--blue)"} strokeWidth="2" strokeLinecap="round" />

          {/* line to load */}
          <line x1="200" y1="60" x2="260" y2="60" stroke={onGen || !onGen ? "var(--green)" : "var(--border-2)"} strokeWidth="1.5" />

          {/* Load */}
          <rect x="260" y="42" width="54" height="36" rx="6" fill="var(--panel)" stroke="var(--border-2)" />
          <text x="287" y="60" textAnchor="middle" fontSize="9" fontFamily="Geist" fill="var(--text-3)" letterSpacing="1">LOAD</text>
          <text x="287" y="73" textAnchor="middle" fontSize="11" fontFamily="JetBrains Mono" fill="var(--text)">{fmt(reading.kw)} kW</text>
        </svg>
      </div>

      <div className="grid g-3" style={{gap: 10}}>
        <div className="hero-stat" style={{border:"1px solid var(--border)", borderRadius: 7, background: "var(--panel)"}}>
          <div className="l">Load</div>
          <div className="v">{Math.round((reading.kw / 300) * 100)}<span className="u">%</span></div>
        </div>
        <div className="hero-stat" style={{border:"1px solid var(--border)", borderRadius: 7, background: "var(--panel)"}}>
          <div className="l">Last transfer</div>
          <div className="v" style={{fontSize:14}}>2h 14m</div>
        </div>
        <div className="hero-stat" style={{border:"1px solid var(--border)", borderRadius: 7, background: "var(--panel)"}}>
          <div className="l">Transfers (30d)</div>
          <div className="v" style={{fontSize:14}}>3</div>
        </div>
      </div>
    </div>
  );
}

// ─── Phase row for electrical card ──────────────────────────────────────────
function PhaseRow({ label, value, unit, pct, color = "var(--accent)" }) {
  return (
    <div className="phase-row">
      <div className="ph">{label}</div>
      <div className="bar"><i style={{ width: `${Math.max(0,Math.min(100,pct))}%`, background: color }} /></div>
      <div className="num">{value}<span className="u">{unit}</span></div>
    </div>
  );
}

// ─── Electrical card ────────────────────────────────────────────────────────
function ElectricalCard({ reading, series, sparks }) {
  const vMax = 500, iMax = 240;
  return (
    <Card title="Generator Output" sub="3-phase · Line-to-Line" actions={<LiveTick rateMs={1500} />}>
      <div className="grid g-2" style={{gap: 16}}>
        <div>
          <div className="label-row" style={{marginBottom: 6}}>
            <span>Voltage L–L</span>
            <span className="mono" style={{textTransform:"none", letterSpacing:0}}>{Math.round((reading.vAB+reading.vBC+reading.vCA)/3)} V <span style={{color:"var(--text-4)"}}>avg</span></span>
          </div>
          <PhaseRow label="A–B" value={fmt(reading.vAB)} unit=" V" pct={reading.vAB/vMax*100} color="var(--green)" />
          <PhaseRow label="B–C" value={fmt(reading.vBC)} unit=" V" pct={reading.vBC/vMax*100} color="var(--amber)" />
          <PhaseRow label="C–A" value={fmt(reading.vCA)} unit=" V" pct={reading.vCA/vMax*100} color="var(--blue)" />
          {sparks && <div style={{marginTop:10, opacity:0.85}}><Sparkline points={series.map(s=>s.vAB)} width={300} height={36} color="var(--text-3)" /></div>}
        </div>
        <div>
          <div className="label-row" style={{marginBottom: 6}}>
            <span>Current per phase</span>
            <span className="mono" style={{textTransform:"none", letterSpacing:0}}>{Math.round((reading.iA+reading.iB+reading.iC)/3)} A <span style={{color:"var(--text-4)"}}>avg</span></span>
          </div>
          <PhaseRow label="A" value={fmt(reading.iA)} unit=" A" pct={reading.iA/iMax*100} color="var(--green)" />
          <PhaseRow label="B" value={fmt(reading.iB)} unit=" A" pct={reading.iB/iMax*100} color="var(--amber)" />
          <PhaseRow label="C" value={fmt(reading.iC)} unit=" A" pct={reading.iC/iMax*100} color="var(--blue)" />
          {sparks && <div style={{marginTop:10, opacity:0.85}}><Sparkline points={series.map(s=>s.iA)} width={300} height={36} color="var(--text-3)" /></div>}
        </div>
      </div>

      <div className="grid g-3" style={{gap: 12, marginTop: 16, paddingTop: 14, borderTop: "1px solid var(--border)"}}>
        <BigMetric label="Frequency" value={reading.hz.toFixed(1)} unit="Hz" tone={Math.abs(reading.hz-60)<0.5 ? "ok" : "warn"} sparkPoints={sparks ? series.map(s=>s.hz) : null} sparkColor="var(--green)" />
        <BigMetric label="Real power" value={fmt(reading.kw)} unit="kW" sparkPoints={sparks ? series.map(s=>s.kw) : null} sparkColor="var(--amber)" />
        <BigMetric label="Apparent" value={fmt(Math.round(reading.kw * 1.07))} unit="kVA" sparkPoints={sparks ? series.map(s=>s.kw*1.07) : null} sparkColor="var(--blue)" />
      </div>
    </Card>
  );
}

function BigMetric({ label, value, unit, tone, sparkPoints, sparkColor }) {
  return (
    <div>
      <div className="label-row">
        <span>{label}</span>
        {tone && <Pill tone={tone}>{tone === "ok" ? "in band" : "review"}</Pill>}
      </div>
      <div className="mono" style={{fontSize: 26, fontWeight: 500, marginTop: 6, letterSpacing: "-0.01em"}}>
        {value}<span style={{fontSize:13, color:"var(--text-3)", marginLeft:4, fontWeight:400}}>{unit}</span>
      </div>
      {sparkPoints && <div style={{marginTop:6, opacity:0.85}}><Sparkline points={sparkPoints} width={220} height={32} color={sparkColor || "var(--text-3)"} /></div>}
    </div>
  );
}

// ─── Engine card ────────────────────────────────────────────────────────────
function EngineCard({ reading, series, sparks }) {
  return (
    <Card title="Engine" sub="Cummins QSB7-G5 · 200 kW" actions={<Pill tone={reading.oilP < 25 && reading.rpm > 100 ? "alarm" : "ok"}>nominal</Pill>}>
      <div className="grid g-4">
        <EngineMetric label="RPM"        value={fmt(reading.rpm)} unit="rpm" min={0}   max={2200} sparkPoints={sparks && series.map(s=>s.rpm)} color="var(--green)" warnRange={[1750, 1850]} />
        <EngineMetric label="Oil pres."  value={reading.oilP.toFixed(0)} unit="psi"  min={0}   max={100}  sparkPoints={sparks && series.map(s=>s.oilP)} color="var(--blue)"  warnRange={[35, 80]} />
        <EngineMetric label="Coolant"    value={reading.coolT.toFixed(0)} unit="°F"  min={50}  max={250}  sparkPoints={sparks && series.map(s=>s.coolT)} color="var(--amber)" warnRange={[170, 210]} />
        <EngineMetric label="Battery"    value={reading.batt.toFixed(2)} unit="V"    min={10}  max={16}   sparkPoints={sparks && series.map(s=>s.batt)} color="var(--text-2)" warnRange={[12.6, 14.4]} />
      </div>
    </Card>
  );
}

function EngineMetric({ label, value, unit, min, max, sparkPoints, color, warnRange }) {
  const numeric = parseFloat(value.replace(/,/g, ""));
  const inBand = warnRange ? numeric >= warnRange[0] && numeric <= warnRange[1] : true;
  return (
    <div style={{padding: "2px 4px"}}>
      <div className="label-row">
        <span>{label}</span>
        <span style={{textTransform:"none", letterSpacing:0, color: inBand ? "var(--text-4)" : "var(--amber)"}} className="mono">
          {warnRange ? `${warnRange[0]}–${warnRange[1]}` : `${min}–${max}`}
        </span>
      </div>
      <div className="mono" style={{fontSize: 26, fontWeight: 500, marginTop: 6, letterSpacing: "-0.01em", color: inBand ? "var(--text)" : "var(--amber)"}}>
        {value}<span style={{fontSize:13, color:"var(--text-3)", marginLeft:4, fontWeight:400}}>{unit}</span>
      </div>
      <div style={{marginTop: 4}}>
        {sparkPoints ? <Sparkline points={sparkPoints} width={200} height={32} color={color} /> : <RangeBar value={numeric} min={min} max={max} warnRange={warnRange} color={color} />}
      </div>
    </div>
  );
}

function RangeBar({ value, min, max, warnRange, color }) {
  const pct = Math.max(0, Math.min(100, ((value - min) / (max - min)) * 100));
  return (
    <div style={{ position:"relative", height: 6, borderRadius: 999, background: "var(--panel-3)", overflow:"hidden" }}>
      {warnRange && (
        <i style={{ position:"absolute", left: `${(warnRange[0]-min)/(max-min)*100}%`, width: `${(warnRange[1]-warnRange[0])/(max-min)*100}%`, top:0,bottom:0, background: "color-mix(in oklch, var(--green) 30%, transparent)" }} />
      )}
      <i style={{ position:"absolute", top:1, bottom:1, left: `calc(${pct}% - 1.5px)`, width: 3, background: color, borderRadius: 2 }} />
    </div>
  );
}

// ─── Controls panel ─────────────────────────────────────────────────────────
function ControlsPanel({ state, onCommand }) {
  const canStart    = state === "stopped";
  const canStop     = state === "running" || state === "exercising" || state === "cranking";
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
      <div style={{marginTop: 14, padding: "10px 12px", background: "var(--panel-2)", borderRadius: 8, border: "1px solid var(--border)", fontSize: 12, color: "var(--text-3)", display:"flex", gap: 10, alignItems:"flex-start"}}>
        <Icon name="lock" size={14} />
        <div>Commands write to <span className="mono" style={{color:"var(--text-2)"}}>0x00A0–A2</span> via FC06. Engine hardware safeties (panel) remain primary.</div>
      </div>
    </Card>
  );
}

// ─── Tank / run-hours / next exercise ───────────────────────────────────────
function FuelMaintCard({ reading }) {
  const fuel = Math.max(0, Math.min(100, reading.fuelPct ?? 78));
  const lowFuel = fuel < 25;
  return (
    <Card title="Tank · Maintenance" sub="Local diesel · 220 gal">
      <div style={{padding: "4px 0 12px"}}>
        <div className="label-row" style={{padding:"0 0 8px"}}>
          <span>Fuel level</span>
          <span className="mono" style={{textTransform:"none", letterSpacing:0, color: lowFuel ? "var(--amber)" : "var(--text-2)"}}>{fuel.toFixed(1)} % · ~{Math.round(fuel*2.2)} gal</span>
        </div>
        <div className="fuel-bar">
          <i style={{ width: `${fuel}%` }} data-low={lowFuel ? "1" : "0"} />
          <div className="ticks">
            {Array.from({length: 9}).map((_,i)=><i key={i}/>)}
          </div>
        </div>
        <div className="flex jc-sb mono" style={{marginTop:6, fontSize:10.5, color:"var(--text-4)"}}>
          <span>0</span><span>25</span><span>50</span><span>75</span><span>100 %</span>
        </div>
      </div>

      <div className="kv" style={{marginTop: 8, paddingTop: 10, borderTop: "1px solid var(--border)"}}>
        <div className="kv-row"><span className="l">Run hours (total)</span><span className="v">1,847.6 h</span></div>
        <div className="kv-row"><span className="l">Since last service</span><span className="v">214.2 h</span></div>
        <div className="kv-row"><span className="l">Next service due</span><span className="v">85.8 h</span></div>
        <div className="kv-row"><span className="l">Next exercise</span><span className="v">Sun · 03:00</span></div>
        <div className="kv-row"><span className="l">Last alarm</span><span className="v">14 m ago</span></div>
      </div>
    </Card>
  );
}

// ─── Events feed (compact for Live) ─────────────────────────────────────────
function EventsFeed({ limit = 8, dense = false }) {
  const list = EVENTS.slice(0, limit);
  return (
    <Card title="Recent Events" actions={<button className="icon-btn" title="Open events log"><Icon name="arrow" size={14} /></button>} flush>
      <div>
        {list.map((e, i) => (
          <div key={i} className="ev-row" style={dense ? {padding:"7px 14px"} : undefined}>
            <span className="ev-time">{e.t}</span>
            <span className="ev-dot" data-sev={e.sev} />
            <span className="ev-type">{e.type}</span>
            <span className="ev-msg" dangerouslySetInnerHTML={{__html: e.msg}} />
            <span className="ev-meta">{e.meta}</span>
          </div>
        ))}
      </div>
    </Card>
  );
}

// ═══════════════════════════════════════════════════════════════════════════
// LIVE DASHBOARD
// ═══════════════════════════════════════════════════════════════════════════
function LiveView({ state, reading, series, sparks, onCommand, hasAlarm, alarmText, timeInState }) {
  return (
    <>
      {hasAlarm && (
        <div className="alarm-strip" role="alert">
          <span className="led" />
          <strong>Active alarm</strong>
          <span>{alarmText}</span>
          <button className="btn btn-danger">Acknowledge</button>
        </div>
      )}

      <StatusHero state={state} reading={reading} timeInState={timeInState} />

      <div className="row" style={{marginTop: "var(--gap)"}}>
        <ElectricalCard reading={reading} series={series} sparks={sparks} />
        <ControlsPanel state={state} onCommand={onCommand} />
      </div>

      <div className="row" style={{marginTop: "var(--gap)"}}>
        <EngineCard reading={reading} series={series} sparks={sparks} />
        <FuelMaintCard reading={reading} />
      </div>

      <div style={{marginTop: "var(--gap)"}}>
        <EventsFeed limit={6} dense />
      </div>
    </>
  );
}

// ═══════════════════════════════════════════════════════════════════════════
// HISTORY
// ═══════════════════════════════════════════════════════════════════════════
function HistoryView({ series }) {
  const [range, setRange] = React.useState("1H");
  const [metric, setMetric] = React.useState("kw");
  const metricDefs = {
    kw:    { label: "Real power",     unit: "kW", color: "var(--amber)" },
    rpm:   { label: "Engine RPM",     unit: "rpm", color: "var(--green)" },
    hz:    { label: "Frequency",      unit: "Hz", color: "var(--green)" },
    oilP:  { label: "Oil pressure",   unit: "psi", color: "var(--blue)" },
    coolT: { label: "Coolant temp",   unit: "°F", color: "var(--amber)" },
    batt:  { label: "Battery",        unit: "V",   color: "var(--text-2)" },
    vAB:   { label: "Voltage A-B",    unit: "V", color: "var(--green)" },
    iA:    { label: "Current A",      unit: "A", color: "var(--blue)" },
  };
  // build longer series for chart
  const longSeries = React.useMemo(() => buildSeries("running", 120, 17), []);
  const data = longSeries.map(s => s[metric]);

  // sample stats
  const min = Math.min(...data);
  const max = Math.max(...data);
  const avg = data.reduce((a,b)=>a+b, 0) / data.length;

  return (
    <>
      <div className="page-head">
        <div>
          <h1 className="page-title">Telemetry History</h1>
          <div className="page-sub">SQLite time-series · raw 7 d, 1-min rollup 90 d · {Object.keys(metricDefs).length} indexed metrics</div>
        </div>
        <div className="flex ai-c gap-8">
          <Pill tone="info">84.1 MB · ↓6.4 KB/s</Pill>
          <button className="btn"><Icon name="download" size={14} /> Export CSV</button>
        </div>
      </div>

      <Card flush>
        <div className="chart-toolbar">
          <div className="flex ai-c gap-12">
            <div className="range-group">
              {["10M","1H","6H","24H","7D","30D"].map(r => (
                <button key={r} aria-current={r === range ? "page" : undefined} onClick={() => setRange(r)}>{r}</button>
              ))}
            </div>
            <span style={{color:"var(--text-3)", fontSize: 12, fontFamily:"var(--mono)"}}>
              {range === "10M" ? "10:23 — 10:33" : range === "1H" ? "09:33 — 10:33" : "today, until now"}
            </span>
          </div>
          <div className="legend"><i style={{background: metricDefs[metric].color}} /> {metricDefs[metric].label}</div>
        </div>

        <div className="metric-tabs">
          {Object.entries(metricDefs).map(([k, m]) => (
            <button key={k} className="metric-tab" aria-current={k === metric ? "true" : undefined} onClick={() => setMetric(k)}>
              <span className="swatch" style={{background: m.color}} />
              {m.label}
            </button>
          ))}
        </div>

        <div className="chart-wrap">
          <LineChart
            series={[{ data, name: metricDefs[metric].label }]}
            colors={[metricDefs[metric].color]}
            xLabels={["09:33","09:42","09:51","10:00","10:09","10:18","10:27","now"]}
            height={320}
          />
        </div>

        <div className="grid g-4" style={{padding: "10px 14px 14px", borderTop:"1px solid var(--border)", gap: 0}}>
          <StatTile label="min" value={fmt(min, metric === "batt" || metric === "hz" ? 2 : 0)} unit={metricDefs[metric].unit} />
          <StatTile label="avg" value={fmt(avg, metric === "batt" || metric === "hz" ? 2 : 0)} unit={metricDefs[metric].unit} />
          <StatTile label="max" value={fmt(max, metric === "batt" || metric === "hz" ? 2 : 0)} unit={metricDefs[metric].unit} />
          <StatTile label="now" value={fmt(data[data.length-1], metric === "batt" || metric === "hz" ? 2 : 0)} unit={metricDefs[metric].unit} live />
        </div>
      </Card>

      <div className="grid g-2" style={{marginTop:"var(--gap)"}}>
        <Card title="Daily energy" sub="last 14 days" actions={<Pill tone="ok">+12% wk/wk</Pill>}>
          <Bars data={[8,12,4,0,0,18,22,9,6,3,28,32,11,7]} colors={["var(--amber)"]} />
        </Card>
        <Card title="Runtime breakdown" sub="last 30 days">
          <Donut segments={[
            { label: "Standby",    value: 612.4, color: "var(--slate)" },
            { label: "On load",    value:  92.1, color: "var(--green)" },
            { label: "Exercise",   value:  15.5, color: "var(--amber)" },
            { label: "Cool-down",  value:   3.8, color: "var(--blue)" },
          ]} />
        </Card>
      </div>
    </>
  );
}

function StatTile({ label, value, unit, live }) {
  return (
    <div style={{padding: "10px 14px", borderRight: "1px solid var(--border)"}}>
      <div className="label-row"><span>{label}</span>{live && <LiveTick rateMs={1500} />}</div>
      <div className="mono" style={{fontSize: 22, fontWeight: 500, marginTop: 4, letterSpacing:"-0.01em"}}>
        {value}<span style={{fontSize: 12, color: "var(--text-3)", marginLeft: 4, fontWeight: 400}}>{unit}</span>
      </div>
    </div>
  );
}

function Bars({ data, colors = ["var(--accent)"] }) {
  const max = Math.max(...data, 1);
  return (
    <div style={{display:"flex", gap: 6, alignItems:"flex-end", height: 120, padding: "8px 0"}}>
      {data.map((v, i) => (
        <div key={i} style={{flex: 1, display:"flex", flexDirection:"column", alignItems:"center", gap: 6}}>
          <div style={{ width: "100%", height: `${(v/max)*100}%`, background: colors[0], borderRadius: 4, opacity: 0.6 + 0.4 * (v/max), minHeight: v ? 4 : 1 }} />
          <span className="mono" style={{fontSize: 9, color: "var(--text-4)"}}>{i + 1}</span>
        </div>
      ))}
    </div>
  );
}

function Donut({ segments }) {
  const total = segments.reduce((a, b) => a + b.value, 0);
  const r = 56, c = 70, cy = 70, sw = 16;
  let acc = 0;
  const circ = 2 * Math.PI * r;
  return (
    <div className="flex ai-c gap-16">
      <svg width="140" height="140" viewBox="0 0 140 140">
        <circle cx={c} cy={cy} r={r} fill="none" stroke="var(--panel-3)" strokeWidth={sw} />
        {segments.map((s, i) => {
          const len = (s.value / total) * circ;
          const el = (
            <circle key={i} cx={c} cy={cy} r={r} fill="none" stroke={s.color} strokeWidth={sw}
                    strokeDasharray={`${len} ${circ - len}`}
                    strokeDashoffset={-acc}
                    transform={`rotate(-90 ${c} ${cy})`} />
          );
          acc += len;
          return el;
        })}
        <text x={c} y={cy - 4} textAnchor="middle" fontSize="20" fontFamily="JetBrains Mono" fill="var(--text)">{total.toFixed(0)}</text>
        <text x={c} y={cy + 14} textAnchor="middle" fontSize="10" fontFamily="Geist" fill="var(--text-3)" letterSpacing="1">HOURS · 30D</text>
      </svg>
      <div style={{flex:1, display:"flex", flexDirection:"column", gap: 8}}>
        {segments.map((s, i) => {
          const pct = ((s.value / total) * 100).toFixed(1);
          return (
            <div key={i} className="flex ai-c jc-sb" style={{fontSize: 12.5}}>
              <span className="flex ai-c gap-8"><i style={{width: 10, height: 10, borderRadius: 3, background: s.color}}/> {s.label}</span>
              <span className="mono" style={{color:"var(--text-3)"}}>{s.value.toFixed(1)} h · {pct}%</span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════
// EVENTS
// ═══════════════════════════════════════════════════════════════════════════
function EventsView() {
  const [filters, setFilters] = React.useState({ alarm: true, warn: true, info: true, ok: true });
  const [typeFilter, setTypeFilter] = React.useState("ALL");
  const toggle = (k) => setFilters(s => ({ ...s, [k]: !s[k] }));
  const types = ["ALL", ...new Set(EVENTS.map(e => e.type))];
  const list = EVENTS.filter(e => filters[e.sev] && (typeFilter === "ALL" || e.type === typeFilter));

  return (
    <>
      <div className="page-head">
        <div>
          <h1 className="page-title">Events &amp; Alarms</h1>
          <div className="page-sub">{list.length} of {EVENTS.length} events · {ACTIVE_ALARMS.length} active alarm</div>
        </div>
        <div className="flex ai-c gap-8">
          <button className="btn btn-ghost"><Icon name="filter" size={14} /> Saved views</button>
          <button className="btn"><Icon name="download" size={14} /> Export</button>
        </div>
      </div>

      {ACTIVE_ALARMS.map((a, i) => (
        <div key={i} className="alarm-strip">
          <span className="led" />
          <strong>{a.code}</strong>
          <span>{a.desc} · raised <span className="mono">{a.since}</span> ago</span>
          <Pill tone="alarm">Active</Pill>
          <button className="btn btn-danger">Acknowledge</button>
        </div>
      ))}

      <Card flush>
        <div className="filters">
          <span style={{color:"var(--text-3)", fontSize: 11, textTransform:"uppercase", letterSpacing:"0.08em", marginRight: 4, alignSelf:"center"}}>Severity</span>
          <button className="filter-chip" aria-pressed={filters.alarm} onClick={() => toggle("alarm")}><i style={{width:6, height:6, borderRadius:"50%", background:"var(--red)"}} /> Alarm</button>
          <button className="filter-chip" aria-pressed={filters.warn} onClick={() => toggle("warn")}><i style={{width:6, height:6, borderRadius:"50%", background:"var(--amber)"}} /> Warn</button>
          <button className="filter-chip" aria-pressed={filters.info} onClick={() => toggle("info")}><i style={{width:6, height:6, borderRadius:"50%", background:"var(--blue)"}} /> Info</button>
          <button className="filter-chip" aria-pressed={filters.ok} onClick={() => toggle("ok")}><i style={{width:6, height:6, borderRadius:"50%", background:"var(--green)"}} /> OK</button>
          <span style={{width: 1, height: 16, background: "var(--border)", margin: "0 4px", alignSelf:"center"}} />
          <span style={{color:"var(--text-3)", fontSize: 11, textTransform:"uppercase", letterSpacing:"0.08em", marginRight: 4, alignSelf:"center"}}>Type</span>
          {types.map(t => (
            <button key={t} className="filter-chip" aria-pressed={t === typeFilter} onClick={() => setTypeFilter(t)}>{t}</button>
          ))}
        </div>
        <div>
          {list.map((e, i) => (
            <div key={i} className="ev-row">
              <span className="ev-time">{e.t}</span>
              <span className="ev-dot" data-sev={e.sev} />
              <span className="ev-type">{e.type}</span>
              <span className="ev-msg" dangerouslySetInnerHTML={{__html: e.msg}} />
              <span className="ev-meta">{e.meta}</span>
            </div>
          ))}
        </div>
      </Card>

      <Card title="Alarm codes reference" sub="from registers/alarms.yaml · editable" actions={<button className="btn btn-ghost"><Icon name="plus" size={14} /> Add code</button>} flush>
        <table className="reg-table">
          <thead><tr><th>Code</th><th>Description</th><th>Severity</th><th>Action</th></tr></thead>
          <tbody>
            {ALARM_CODES.map((a, i) => (
              <tr key={i}>
                <td className="mono">{a.code}</td>
                <td>{a.desc}</td>
                <td><Pill tone={a.sev === "alarm" ? "alarm" : "warn"}>{a.sev}</Pill></td>
                <td className="mono" style={{color:"var(--text-3)"}}>notify all</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>
    </>
  );
}

// ═══════════════════════════════════════════════════════════════════════════
// SETTINGS
// ═══════════════════════════════════════════════════════════════════════════
function SettingsView() {
  const [section, setSection] = React.useState("serial");
  const [serial, setSerial] = React.useState({ device: "/dev/ttyUSB0", baud: "9600", parity: "N", stop: "1", bytesize: "8", timeout: "1.5" });
  const [modbus, setModbus] = React.useState({ slave: "100", regFile: "registers/h100.yaml", readFc: "0x03", primeMs: "1500", baseMs: "15000" });
  const [notif, setNotif] = React.useState({ mqtt: true, smtp: true, sms: false, ntfy: true, snmp: false });

  const sections = [
    { id: "serial",   label: "Serial Port",      icon: "cable" },
    { id: "modbus",   label: "Modbus",           icon: "cpu" },
    { id: "registers",label: "Register Map",     icon: "list" },
    { id: "notif",    label: "Notifications",    icon: "bell" },
    { id: "users",    label: "Users & Access",   icon: "user" },
    { id: "retent",   label: "Retention",        icon: "history" },
  ];

  return (
    <>
      <div className="page-head">
        <div>
          <h1 className="page-title">Settings</h1>
          <div className="page-sub">Single-file config · validated against schema · changes apply on save</div>
        </div>
        <div className="flex ai-c gap-8">
          <button className="btn btn-ghost">Discard</button>
          <button className="btn btn-primary">Save &amp; reload poller</button>
        </div>
      </div>

      <div className="settings-grid">
        <nav className="settings-side">
          {sections.map(s => (
            <button key={s.id} aria-current={s.id === section ? "page" : undefined} onClick={() => setSection(s.id)}>
              <Icon name={s.icon} size={14} /> {s.label}
            </button>
          ))}
        </nav>
        <div>
          {section === "serial" && <SerialSection v={serial} set={setSerial} />}
          {section === "modbus" && <ModbusSection v={modbus} set={setModbus} />}
          {section === "registers" && <RegisterMapSection />}
          {section === "notif" && <NotifSection v={notif} set={setNotif} />}
          {section === "users" && <UsersSection />}
          {section === "retent" && <RetentionSection />}
        </div>
      </div>
    </>
  );
}

function SerialSection({ v, set }) {
  return (
    <div className="settings-section">
      <div className="settings-head">
        <h2>Serial port</h2>
        <p>USB-RS232 adapter. Must match the panel's front-panel/GenLink settings.</p>
      </div>
      <div className="field-row"><div className="lbl">Device <span className="desc">/dev/serial0 for onboard UART</span></div><input className="input" value={v.device} onChange={e => set({...v, device: e.target.value})}/></div>
      <div className="field-row"><div className="lbl">Baud rate</div>
        <select className="select" value={v.baud} onChange={e => set({...v, baud: e.target.value})}>
          {["1200","2400","4800","9600","19200","38400","57600","115200"].map(b => <option key={b}>{b}</option>)}
        </select>
      </div>
      <div className="field-row"><div className="lbl">Parity · Stop · Data <span className="desc">8N1 is the H-100 default</span></div>
        <div style={{display:"grid", gridTemplateColumns:"1fr 1fr 1fr", gap: 8}}>
          <select className="select" value={v.parity} onChange={e=>set({...v,parity:e.target.value})}><option>N</option><option>E</option><option>O</option></select>
          <select className="select" value={v.stop} onChange={e=>set({...v,stop:e.target.value})}><option>1</option><option>2</option></select>
          <select className="select" value={v.bytesize} onChange={e=>set({...v,bytesize:e.target.value})}><option>7</option><option>8</option></select>
        </div>
      </div>
      <div className="field-row"><div className="lbl">Timeout <span className="desc">seconds; per request</span></div><input className="input" value={v.timeout} onChange={e=>set({...v,timeout:e.target.value})}/></div>
      <div className="field-row">
        <div className="lbl">Connection test</div>
        <div className="flex ai-c gap-12">
          <button className="btn"><Icon name="activity" size={14} /> Run modbusdump</button>
          <Pill tone="ok">last test 8s ago · 12/12 ok</Pill>
        </div>
      </div>
    </div>
  );
}

function ModbusSection({ v, set }) {
  return (
    <>
      <div className="settings-section">
        <div className="settings-head">
          <h2>Modbus protocol</h2>
          <p>Function codes &amp; addressing for the H-100 slave at <span className="mono">{v.slave}</span> (0x{Number(v.slave).toString(16).padStart(2,"0").toUpperCase()}).</p>
        </div>
        <div className="field-row"><div className="lbl">Slave address</div><input className="input" value={v.slave} onChange={e=>set({...v,slave:e.target.value})}/></div>
        <div className="field-row"><div className="lbl">Register map file <span className="desc">YAML, hot-reloadable</span></div><input className="input" value={v.regFile} onChange={e=>set({...v,regFile:e.target.value})}/></div>
        <div className="field-row"><div className="lbl">Read function code <span className="desc">Most H-100s answer 0x03</span></div>
          <select className="select" value={v.readFc} onChange={e=>set({...v,readFc:e.target.value})}>
            <option value="0x03">0x03 — Read Holding Registers</option>
            <option value="0x04">0x04 — Read Input Registers</option>
          </select>
        </div>
        <div className="field-row"><div className="lbl">Prime poll interval <span className="desc">state &amp; alarm registers</span></div>
          <div style={{display:"flex", gap:8, alignItems:"center"}}><input className="input" value={v.primeMs} onChange={e=>set({...v,primeMs:e.target.value})}/><span className="mono" style={{fontSize: 12, color:"var(--text-3)"}}>ms</span></div>
        </div>
        <div className="field-row"><div className="lbl">Base poll interval <span className="desc">slow-changing telemetry</span></div>
          <div style={{display:"flex", gap:8, alignItems:"center"}}><input className="input" value={v.baseMs} onChange={e=>set({...v,baseMs:e.target.value})}/><span className="mono" style={{fontSize: 12, color:"var(--text-3)"}}>ms</span></div>
        </div>
      </div>
    </>
  );
}

function RegisterMapSection() {
  return (
    <Card title="Register map — h100.yaml" sub="rev 14 · 12 registers · 3 control writes" actions={<><button className="btn btn-ghost"><Icon name="refresh" size={14} /> Reload</button><button className="btn"><Icon name="plus" size={14} /> Add register</button></>} flush>
      <table className="reg-table">
        <thead>
          <tr><th>Address</th><th>Name</th><th>FC</th><th>Type</th><th>Scale</th><th>Unit</th><th>Last read</th></tr>
        </thead>
        <tbody>
          {REGISTERS.map((r, i) => (
            r.group
              ? <tr key={i} className="group"><td colSpan={7}>{r.group}</td></tr>
              : (
                <tr key={i}>
                  <td className="mono">{r.addr}</td>
                  <td className="mono" style={{color:"var(--text)"}}>{r.name}</td>
                  <td className="mono">{r.fc}</td>
                  <td className="mono" style={{color:"var(--text-3)"}}>{r.type}</td>
                  <td className="mono" style={{color:"var(--text-3)"}}>{r.scale || "—"}</td>
                  <td>{r.unit}</td>
                  <td className="mono" style={{color:"var(--text)"}}>{r.value}</td>
                </tr>
              )
          ))}
        </tbody>
      </table>
    </Card>
  );
}

function NotifSection({ v, set }) {
  const channels = [
    { id: "mqtt", title: "MQTT", desc: "broker.local:1883 · HA discovery on · genwatch/#" },
    { id: "smtp", title: "Email (SMTP)", desc: "alerts@example.com via smtp.example.com:587" },
    { id: "sms",  title: "SMS (Twilio)", desc: "+1 415-···-·· · alarm-severity only" },
    { id: "ntfy", title: "Push (ntfy.sh)", desc: "ntfy.sh/genwatch-x9q2 · all events" },
    { id: "snmp", title: "SNMP trap",     desc: "192.168.10.5:162 · v2c · public" },
  ];
  return (
    <div className="settings-section">
      <div className="settings-head">
        <h2>Notifications</h2>
        <p>Each channel subscribes to the event bus independently. Quiet hours and debounce apply per-channel.</p>
      </div>
      {channels.map(c => (
        <div key={c.id} className="field-row">
          <div className="lbl">{c.title}<span className="desc">{c.desc}</span></div>
          <div className="flex ai-c jc-sb">
            <button className="btn btn-ghost" style={{padding:"6px 8px"}}>Configure →</button>
            <Switch value={v[c.id]} onChange={(x) => set({...v, [c.id]: x})} />
          </div>
        </div>
      ))}
    </div>
  );
}

function UsersSection() {
  const users = [
    { name: "Kim Harris",   email: "kim.harris@example.com",  role: "admin",    last: "now" },
    { name: "Drew Park",    email: "drew@example.com",        role: "operator", last: "2 h ago" },
    { name: "Site monitor", email: "—",                       role: "viewer",   last: "4 d ago" },
  ];
  return (
    <div className="settings-section">
      <div className="settings-head">
        <h2>Users &amp; access</h2>
        <p>Three roles. Viewer can read; operator can command; admin can configure. All control actions are audit-logged.</p>
      </div>
      <table className="reg-table" style={{borderTop: 0}}>
        <thead><tr><th>Name</th><th>Email</th><th>Role</th><th>Last seen</th><th></th></tr></thead>
        <tbody>
          {users.map((u, i) => (
            <tr key={i}>
              <td style={{color:"var(--text)"}}>{u.name}</td>
              <td className="mono" style={{color:"var(--text-3)"}}>{u.email}</td>
              <td><Pill tone={u.role === "admin" ? "info" : u.role === "operator" ? "ok" : "warn"}>{u.role}</Pill></td>
              <td className="mono" style={{color:"var(--text-3)"}}>{u.last}</td>
              <td style={{textAlign:"right"}}><button className="btn btn-ghost" style={{padding:"4px 8px"}}>Edit</button></td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="field-row">
        <div className="lbl">Add user</div>
        <div className="flex ai-c gap-8"><button className="btn"><Icon name="plus" size={14} /> Invite user</button><span className="text-fa" style={{fontSize: 12}}>Send a one-time bootstrap link</span></div>
      </div>
      <div className="field-row">
        <div className="lbl">Tailscale ACL <span className="desc">tag:genwatch — restrict reachable nodes</span></div>
        <Pill tone="ok">Synced · 4 devices</Pill>
      </div>
    </div>
  );
}

function RetentionSection() {
  return (
    <div className="settings-section">
      <div className="settings-head">
        <h2>Storage &amp; retention</h2>
        <p>Bounded telemetry table with downsampling. SQLite in WAL mode at <span className="mono">/var/lib/genwatch/db.sqlite</span>.</p>
      </div>
      <div className="field-row"><div className="lbl">Raw telemetry <span className="desc">prime+base, every poll</span></div>
        <div className="flex ai-c gap-8"><input className="input" defaultValue="7" /><span className="mono" style={{fontSize: 12, color:"var(--text-3)"}}>days</span></div>
      </div>
      <div className="field-row"><div className="lbl">1-minute rollups</div>
        <div className="flex ai-c gap-8"><input className="input" defaultValue="90" /><span className="mono" style={{fontSize: 12, color:"var(--text-3)"}}>days</span></div>
      </div>
      <div className="field-row"><div className="lbl">1-hour rollups</div>
        <div className="flex ai-c gap-8"><input className="input" defaultValue="730" /><span className="mono" style={{fontSize: 12, color:"var(--text-3)"}}>days</span></div>
      </div>
      <div className="field-row"><div className="lbl">Event log</div>
        <div className="flex ai-c gap-8"><input className="input" defaultValue="never" /><span style={{fontSize: 12, color:"var(--text-3)"}}>(events &amp; audit are append-only)</span></div>
      </div>
      <div className="field-row">
        <div className="lbl">Disk usage</div>
        <div>
          <div className="fuel-bar" style={{height: 8}}><i style={{width: "12%"}} /></div>
          <div className="flex jc-sb mono" style={{fontSize: 11, color:"var(--text-3)", marginTop: 6}}><span>84.1 MB · 12%</span><span>720 MB free of 800 MB partition</span></div>
        </div>
      </div>
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════
// CONFIRM MODAL
// ═══════════════════════════════════════════════════════════════════════════
function ConfirmModal({ command, onClose, onConfirm }) {
  const [token, setToken] = React.useState("");
  const [confirmed, setConfirmed] = React.useState(false);
  React.useEffect(() => {
    if (!command) return;
    setToken("");
    setConfirmed(false);
    // simulate confirm-token fetch
    const t = setTimeout(() => setToken(Math.random().toString(16).slice(2, 10).toUpperCase()), 320);
    return () => clearTimeout(t);
  }, [command]);
  const specs = {
    start:    { title: "Confirm Remote Start",     verb: "Start", tone: "start", danger: false,
                bullets: ["Engine will crank within 2 seconds", "HTS-1 will transfer to GENERATOR", "Run hours will accumulate"] },
    stop:     { title: "Confirm Remote Stop",      verb: "Stop",  tone: "stop",  danger: true,
                bullets: ["HTS-1 will transfer back to UTILITY", "Engine enters 5-minute cool-down", "Site briefly on utility-only"] },
    exercise: { title: "Confirm Quiet-Test",       verb: "Start exercise", tone: "exer", danger: false,
                bullets: ["Engine runs unloaded for 30:00", "No transfer · utility remains primary", "Sound profile: quiet mode"] },
    transfer: { title: "Confirm Transfer Back",    verb: "Transfer", tone: "xfer", danger: false,
                bullets: ["HTS-1 → UTILITY", "Engine continues running through cool-down", "Brief 100-200 ms power gap on load"] },
  };
  const c = specs[command];
  return (
    <Modal open={!!command} onClose={onClose} title={c?.title} sub={`Two-step confirm · audit-logged · operator: kim.harris`}
      footer={<>
        <button className="btn btn-ghost" onClick={onClose}>Cancel</button>
        <button className={c?.danger ? "btn btn-danger" : "btn btn-primary"} disabled={!confirmed || !token} onClick={onConfirm}>
          <Icon name="check" size={14} /> {c?.verb}
        </button>
      </>}
    >
      <div style={{marginBottom: 14}}>
        {c?.bullets.map((b, i) => (
          <div key={i} className="check-line on">
            <span className="cb"><Icon name="check" size={12} stroke={2.6} /></span>
            <div><div className="lbl">{b}</div></div>
          </div>
        ))}
      </div>
      <div className="check-line" style={{cursor:"pointer"}} onClick={() => setConfirmed(c => !c)}>
        <span className="cb" style={confirmed ? {borderColor:"var(--green)", background:"var(--green)", color:"var(--bg)"} : null}>{confirmed && <Icon name="check" size={12} stroke={2.6} />}</span>
        <div>
          <div className="lbl">I understand this will physically affect the generator and load.</div>
          <div className="desc">Hardware safeties at the H-100 panel remain primary.</div>
        </div>
      </div>
      <div style={{marginTop: 12, padding: 10, background: "var(--panel-2)", borderRadius: 7, border: "1px solid var(--border)", display:"flex", justifyContent:"space-between", alignItems:"center", fontSize: 12}}>
        <span className="text-fa">Confirm token (valid 30 s)</span>
        <span className="mono" style={{color:"var(--text)", fontSize: 13}}>{token || "…"}</span>
      </div>
    </Modal>
  );
}

Object.assign(window, { LiveView, HistoryView, EventsView, SettingsView, ConfirmModal });
