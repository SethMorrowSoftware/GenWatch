// Main App shell — routing, tweaks, live polling simulation.

const TWEAK_DEFAULTS = /*EDITMODE-BEGIN*/{
  "theme": "dark",
  "density": "regular",
  "engineState": "running",
  "commsState": "healthy",
  "sparklines": true,
  "showAlarm": false,
  "rateMs": 1500
}/*EDITMODE-END*/;

function useClock() {
  const [now, setNow] = React.useState(new Date());
  React.useEffect(() => {
    const t = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(t);
  }, []);
  return now;
}

function useTimeInState(state) {
  const [start] = React.useState(() => {
    // pick a plausible "running for" depending on state
    const off = ({ running: 4*3600+23*60+17, exercising: 12*60+8, cranking: 8, cooling: 3*60+12, alarm: 14*60+23, stopped: 47*60 })[state] || 600;
    return Date.now() - off * 1000;
  });
  const [tick, setTick] = React.useState(0);
  React.useEffect(() => {
    const t = setInterval(() => setTick(x => x + 1), 1000);
    return () => clearInterval(t);
  }, []);
  const sec = Math.floor((Date.now() - start) / 1000);
  const h = Math.floor(sec / 3600).toString().padStart(2, "0");
  const m = Math.floor((sec % 3600) / 60).toString().padStart(2, "0");
  const s = (sec % 60).toString().padStart(2, "0");
  return `${h}:${m}:${s}`;
}

function App() {
  const [t, setTweak] = useTweaks(TWEAK_DEFAULTS);
  const [view, setView] = React.useState("live");
  const [confirmCmd, setConfirmCmd] = React.useState(null);
  const [seed, setSeed] = React.useState(0);
  const now = useClock();
  const timeInState = useTimeInState(t.engineState);

  // apply theme/density to root
  React.useEffect(() => {
    document.documentElement.setAttribute("data-theme", t.theme);
    document.documentElement.setAttribute("data-density", t.density);
  }, [t.theme, t.density]);

  // tick telemetry seed
  React.useEffect(() => {
    const id = setInterval(() => setSeed(x => x + 1), t.rateMs);
    return () => clearInterval(id);
  }, [t.rateMs]);

  const series  = React.useMemo(() => buildSeries(t.engineState, 40, seed), [t.engineState, seed]);
  const reading = series[series.length - 1];

  const onCommand = (cmd) => setConfirmCmd(cmd);
  const handleConfirm = () => {
    const cmd = confirmCmd;
    setConfirmCmd(null);
    if (!cmd) return;
    if (cmd === "start") setTweak("engineState", "cranking");
    if (cmd === "stop")  setTweak("engineState", "cooling");
    if (cmd === "exercise") setTweak("engineState", "exercising");
    if (cmd === "transfer") setTweak("engineState", "cooling");
  };

  // auto-progress state transitions for realism
  React.useEffect(() => {
    if (t.engineState === "cranking") {
      const id = setTimeout(() => setTweak("engineState", "running"), 4000);
      return () => clearTimeout(id);
    }
    if (t.engineState === "cooling") {
      const id = setTimeout(() => setTweak("engineState", "stopped"), 6000);
      return () => clearTimeout(id);
    }
  }, [t.engineState]);

  const navItems = [
    { id: "live",     label: "Live",     icon: "activity" },
    { id: "history",  label: "History",  icon: "history" },
    { id: "events",   label: "Events",   icon: "bell" },
    { id: "settings", label: "Settings", icon: "settings" },
  ];

  const dateStr = now.toLocaleString("en-US", { month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <div className="brand-mark" />
          <div className="brand-name">GenWatch <span>v0.1</span></div>
        </div>
        <nav className="nav" role="tablist">
          {navItems.map(n => (
            <button key={n.id} aria-current={view === n.id ? "page" : undefined} onClick={() => setView(n.id)} role="tab">
              <Icon name={n.icon} size={13} stroke={1.8} />
              {n.label}
              {n.id === "events" && ACTIVE_ALARMS.length > 0 && (
                <span style={{ marginLeft: 4, width: 16, height: 16, borderRadius: 999, background: "var(--red)", color: "white", fontSize: 10, fontWeight: 600, display:"inline-flex", alignItems:"center", justifyContent:"center" }}>{ACTIVE_ALARMS.length}</span>
              )}
            </button>
          ))}
        </nav>
        <div className="topbar-right">
          <div className="comms-badge" data-state={t.commsState}>
            <span className="pulse" />
            <span>Comms · {t.commsState === "healthy" ? "100.0%" : t.commsState === "degraded" ? "91.4%" : "0.0%"}</span>
            <span className="mono">{t.commsState === "lost" ? "—" : `${(t.rateMs/1000).toFixed(1)}s`}</span>
          </div>
          <span className="clock">{dateStr}</span>
          <div className="user-chip">
            <span className="avatar">KH</span>
            Kim Harris <span className="role">admin</span>
          </div>
        </div>
      </header>

      <main className="main">
        {view === "live" && (
          <>
            <div className="page-head">
              <div>
                <h1 className="page-title">Site overview</h1>
                <div className="page-sub">SITE-23 · Generac H-100 · 200 kW · Cummins QSB7-G5 · last sync <span className="mono">{(t.rateMs/1000).toFixed(1)} s ago</span></div>
              </div>
              <div className="flex ai-c gap-8">
                <Pill tone="info">Auto · 03:00 Sun exercise</Pill>
                <Pill tone="ok">2 HTS · 0 annunciators</Pill>
              </div>
            </div>
            <LiveView
              state={t.engineState}
              reading={reading}
              series={series}
              sparks={t.sparklines}
              onCommand={onCommand}
              hasAlarm={t.showAlarm}
              alarmText={ACTIVE_ALARMS[0]?.desc + " · raised " + ACTIVE_ALARMS[0]?.since + " ago"}
              timeInState={timeInState}
            />
          </>
        )}

        {view === "history"  && <HistoryView series={series} />}
        {view === "events"   && <EventsView />}
        {view === "settings" && <SettingsView />}
      </main>

      <footer className="foot">
        <span>GenWatch · running on raspberry-pi-4 · uvicorn 0.32 · pymodbus 3.7 · python 3.11</span>
        <span className="mono">/dev/ttyUSB0 · 9600 8N1 · slave 100 (0x64) · 1,847.6 h</span>
      </footer>

      <ConfirmModal command={confirmCmd} onClose={() => setConfirmCmd(null)} onConfirm={handleConfirm} />

      <TweaksPanel title="GenWatch · Tweaks">
        <TweakSection label="Display" />
        <TweakRadio label="Theme" value={t.theme} options={["dark", "light"]} onChange={(v) => setTweak("theme", v)} />
        <TweakRadio label="Density" value={t.density} options={["compact", "regular", "comfy"]} onChange={(v) => setTweak("density", v)} />
        <TweakToggle label="Sparklines" value={t.sparklines} onChange={(v) => setTweak("sparklines", v)} />

        <TweakSection label="Simulate" />
        <TweakSelect label="Engine state" value={t.engineState}
          options={["stopped", "cranking", "running", "exercising", "cooling", "alarm"]}
          onChange={(v) => setTweak("engineState", v)} />
        <TweakRadio label="Comms" value={t.commsState} options={["healthy", "degraded", "lost"]} onChange={(v) => setTweak("commsState", v)} />
        <TweakToggle label="Active alarm" value={t.showAlarm} onChange={(v) => setTweak("showAlarm", v)} />
        <TweakSlider label="Poll rate" value={t.rateMs} min={500} max={5000} step={100} unit="ms" onChange={(v) => setTweak("rateMs", v)} />

        <TweakSection label="Quick jump" />
        <div style={{display:"grid", gridTemplateColumns:"1fr 1fr", gap:6}}>
          {navItems.map(n => (
            <button key={n.id} className="twk-btn secondary" onClick={() => setView(n.id)}>{n.label}</button>
          ))}
        </div>
      </TweaksPanel>
    </div>
  );
}

ReactDOM.createRoot(document.getElementById("root")).render(<App />);
