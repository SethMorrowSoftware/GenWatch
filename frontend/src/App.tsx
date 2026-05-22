// Top-level app: auth gate, topbar + nav + footer, view switching.

import { useEffect, useState } from "react";
import { api } from "./api/client";
import { Icon } from "./components/primitives";
import { useLiveData } from "./hooks/useLiveData";
import type { MeBody } from "./types";
import { EventsView } from "./views/EventsView";
import { HistoryView } from "./views/HistoryView";
import { LiveView } from "./views/LiveView";
import { LoginView } from "./views/LoginView";
import { SettingsView } from "./views/SettingsView";

type View = "live" | "history" | "events" | "settings";

export function App() {
  const [auth, setAuth] = useState<MeBody | null>(null);
  const [view, setView] = useState<View>("live");

  useEffect(() => {
    api.me().then(setAuth).catch(() => setAuth({ authenticated: false }));
  }, []);

  if (!auth) {
    return <div style={{ padding: 24, color: "var(--text-3)" }}>Loading…</div>;
  }
  if (!auth.authenticated) {
    return <LoginView onLoggedIn={() => api.me().then(setAuth)} />;
  }
  return <Shell auth={auth} view={view} setView={setView} onLogout={async () => { await api.logout(); setAuth({ authenticated: false }); }} />;
}

function Shell({ auth, view, setView, onLogout }: {
  auth: MeBody; view: View; setView: (v: View) => void; onLogout: () => Promise<void>;
}) {
  const live = useLiveData();
  const [clock, setClock] = useState(new Date());

  useEffect(() => {
    const t = setInterval(() => setClock(new Date()), 1000);
    return () => clearInterval(t);
  }, []);

  // Re-render once a second so the time-in-state ticks
  useEffect(() => {
    const t = setInterval(() => setClock((c) => new Date(c.getTime() + 1)), 1000);
    return () => clearInterval(t);
  }, []);

  const navItems: Array<{ id: View; label: string; icon: any }> = [
    { id: "live", label: "Live", icon: "activity" },
    { id: "history", label: "History", icon: "history" },
    { id: "events", label: "Events", icon: "bell" },
    { id: "settings", label: "Settings", icon: "settings" },
  ];

  const status = live.status;
  const comms = status?.comms;
  const activeAlarmCount = status?.activeAlarms.length ?? 0;
  const dateStr = clock.toLocaleString("en-US", {
    month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit",
    second: "2-digit", hour12: false,
  });

  // Bump timeInState locally each second between WS pushes for smooth UI.
  const tickedStatus = status ? {
    ...status,
    timeInState: status.timeInState + Math.floor((Date.now() / 1000) - status.serverTs),
  } : null;

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <div className="brand-mark" />
          <div className="brand-name">GenWatch <span>v0.1</span></div>
        </div>
        <nav className="nav" role="tablist">
          {navItems.map((n) => (
            <button key={n.id} aria-current={view === n.id ? "page" : undefined}
                    onClick={() => setView(n.id)} role="tab">
              <Icon name={n.icon} size={13} stroke={1.8} />
              {n.label}
              {n.id === "events" && activeAlarmCount > 0 && (
                <span style={{
                  marginLeft: 4, width: 16, height: 16, borderRadius: 999,
                  background: "var(--red)", color: "white", fontSize: 10,
                  fontWeight: 600, display: "inline-flex", alignItems: "center", justifyContent: "center",
                }}>{activeAlarmCount}</span>
              )}
            </button>
          ))}
        </nav>
        <div className="topbar-right">
          <div className="comms-badge" data-state={comms?.state ?? "lost"}>
            <span className="pulse" />
            <span>Comms · {comms ? `${comms.successPct.toFixed(1)}%` : "—"}</span>
            <span className="mono">{comms ? `${(comms.rateMs / 1000).toFixed(1)}s` : "—"}</span>
          </div>
          <span className="clock">{dateStr}</span>
          <div className="user-chip" onClick={onLogout} title="Sign out" style={{ cursor: "pointer" }}>
            <span className="avatar">{(auth.operator ?? "??").slice(0, 2).toUpperCase()}</span>
            {auth.operator ?? "operator"} <span className="role">{auth.role ?? "viewer"}</span>
          </div>
        </div>
      </header>

      <main className="main">
        {live.loading && !tickedStatus && (
          <div style={{ padding: 24, color: "var(--text-3)" }}>Connecting to GenWatch…</div>
        )}
        {live.error && (
          <div className="alarm-strip" style={{ marginBottom: 14 }}>
            <span className="led" />
            <strong>Connection error</strong>
            <span>{live.error}</span>
          </div>
        )}
        {tickedStatus && view === "live" && <LiveView status={tickedStatus} history={live.history} />}
        {tickedStatus && view === "history" && <HistoryView />}
        {tickedStatus && view === "events" && <EventsView />}
        {tickedStatus && view === "settings" && <SettingsView />}
      </main>

      <footer className="foot">
        <span>
          GenWatch · running on raspberry-pi · python 3.11 · uvicorn · pymodbus
        </span>
        <span className="mono">
          {comms?.state === "lost" ? "comms lost" : `prime ${(comms?.rateMs ?? 1500) / 1000}s`}
          {" · "}
          {tickedStatus ? `${tickedStatus.site.id} · ${tickedStatus.site.name}` : ""}
        </span>
      </footer>
    </div>
  );
}
