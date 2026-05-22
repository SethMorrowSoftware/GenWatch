// Settings view: serial port, modbus, register map, retention.
// Saves go through PUT /api/config (admin-only) and require a restart for
// serial/modbus changes — we surface that warning rather than try to
// hot-reload the poller.

import { useEffect, useState } from "react";
import { api } from "../api/client";
import { Card, Icon, Pill } from "../components/primitives";

type Section = "serial" | "modbus" | "registers" | "retention";

interface Config {
  configPath: string;
  mock: boolean;
  serial: { device: string; baud: number; parity: string; stopbits: number; bytesize: number; timeout_s: number };
  modbus: { slave: number; read_fc: number; prime_poll_ms: number; base_poll_ms: number; retries: number; register_file: string };
  retention: { raw_days: number; rollup_1m_days: number; rollup_1h_days: number; audit_days: number };
  auth: { operatorName: string; sessionHours: number; passwordConfigured: boolean; jwtSecretConfigured: boolean };
}

export function SettingsView() {
  const [section, setSection] = useState<Section>("serial");
  const [cfg, setCfg] = useState<Config | null>(null);
  const [dirty, setDirty] = useState<Partial<{ serial: any; modbus: any; retention: any }>>({});
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.config().then(setCfg).catch((e) => setError(e?.message ?? "failed to load config"));
  }, []);

  if (!cfg) return <div style={{ padding: 24 }}>Loading settings…</div>;

  const effective = {
    serial: { ...cfg.serial, ...(dirty.serial || {}) },
    modbus: { ...cfg.modbus, ...(dirty.modbus || {}) },
    retention: { ...cfg.retention, ...(dirty.retention || {}) },
  };
  const hasDirty = Object.keys(dirty).length > 0;

  const save = async () => {
    setSaving(true);
    setError(null);
    setSaved(null);
    try {
      const r = await api.updateConfig(dirty as any);
      setSaved(r.restart_required
        ? "Saved. Restart genwatch.service for changes to take effect."
        : "Saved.");
      setDirty({});
      const fresh = await api.config();
      setCfg(fresh);
    } catch (e: any) {
      setError(e?.body?.detail ?? e?.message ?? "save failed");
    } finally {
      setSaving(false);
    }
  };

  const sections: Array<{ id: Section; label: string; icon: any }> = [
    { id: "serial", label: "Serial Port", icon: "cable" },
    { id: "modbus", label: "Modbus", icon: "cpu" },
    { id: "registers", label: "Register Map", icon: "list" },
    { id: "retention", label: "Retention", icon: "history" },
  ];

  return (
    <>
      <div className="page-head">
        <div>
          <h1 className="page-title">Settings</h1>
          <div className="page-sub">
            {cfg.mock ? <span style={{ color: "var(--amber)" }}>MOCK mode (no real serial) · </span> : null}
            Config at <span className="mono">{cfg.configPath || "(env-only)"}</span>
          </div>
        </div>
        <div className="flex ai-c gap-8">
          {saved && <Pill tone="ok">{saved}</Pill>}
          {error && <Pill tone="alarm">{error}</Pill>}
          <button className="btn btn-ghost" disabled={!hasDirty || saving} onClick={() => setDirty({})}>Discard</button>
          <button className="btn btn-primary" disabled={!hasDirty || saving} onClick={save}>
            {saving ? "Saving…" : "Save & reload"}
          </button>
        </div>
      </div>

      <div className="settings-grid">
        <nav className="settings-side">
          {sections.map((s) => (
            <button key={s.id} aria-current={s.id === section ? "page" : undefined} onClick={() => setSection(s.id)}>
              <Icon name={s.icon} size={14} /> {s.label}
            </button>
          ))}
        </nav>
        <div>
          {section === "serial" && (
            <SerialSection
              v={effective.serial}
              set={(patch) => setDirty((d) => ({ ...d, serial: { ...(d.serial || {}), ...patch } }))}
            />
          )}
          {section === "modbus" && (
            <ModbusSection
              v={effective.modbus}
              set={(patch) => setDirty((d) => ({ ...d, modbus: { ...(d.modbus || {}), ...patch } }))}
            />
          )}
          {section === "registers" && <RegisterMapSection />}
          {section === "retention" && (
            <RetentionSection
              v={effective.retention}
              set={(patch) => setDirty((d) => ({ ...d, retention: { ...(d.retention || {}), ...patch } }))}
            />
          )}
        </div>
      </div>
    </>
  );
}

function SerialSection({ v, set }: { v: Config["serial"]; set: (patch: Partial<Config["serial"]>) => void }) {
  return (
    <div className="settings-section">
      <div className="settings-head">
        <h2>Serial port</h2>
        <p>USB-RS485 adapter. Restart required after changes.</p>
      </div>
      <div className="field-row">
        <div className="lbl">Device <span className="desc">/dev/ttyUSB0 or /dev/serial0</span></div>
        <input className="input" value={v.device} onChange={(e) => set({ device: e.target.value })} />
      </div>
      <div className="field-row">
        <div className="lbl">Baud rate</div>
        <select className="select" value={v.baud} onChange={(e) => set({ baud: Number(e.target.value) })}>
          {[1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200].map((b) => <option key={b}>{b}</option>)}
        </select>
      </div>
      <div className="field-row">
        <div className="lbl">Parity · Stop · Data <span className="desc">8N1 is the H-100 default</span></div>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 8 }}>
          <select className="select" value={v.parity} onChange={(e) => set({ parity: e.target.value })}>
            <option>N</option><option>E</option><option>O</option>
          </select>
          <select className="select" value={v.stopbits} onChange={(e) => set({ stopbits: Number(e.target.value) })}>
            <option>1</option><option>2</option>
          </select>
          <select className="select" value={v.bytesize} onChange={(e) => set({ bytesize: Number(e.target.value) })}>
            <option>7</option><option>8</option>
          </select>
        </div>
      </div>
      <div className="field-row">
        <div className="lbl">Timeout <span className="desc">seconds; per request</span></div>
        <input className="input" type="number" step="0.1" value={v.timeout_s}
               onChange={(e) => set({ timeout_s: Number(e.target.value) })} />
      </div>
    </div>
  );
}

function ModbusSection({ v, set }: { v: Config["modbus"]; set: (patch: Partial<Config["modbus"]>) => void }) {
  return (
    <div className="settings-section">
      <div className="settings-head">
        <h2>Modbus protocol</h2>
        <p>Function codes &amp; addressing for the H-100 slave at <span className="mono">{v.slave}</span> (0x{v.slave.toString(16).padStart(2, "0").toUpperCase()}).</p>
      </div>
      <div className="field-row">
        <div className="lbl">Slave address</div>
        <input className="input" type="number" value={v.slave} onChange={(e) => set({ slave: Number(e.target.value) })} />
      </div>
      <div className="field-row">
        <div className="lbl">Register map file <span className="desc">YAML, hot-reloadable</span></div>
        <input className="input" value={v.register_file} onChange={(e) => set({ register_file: e.target.value })} />
      </div>
      <div className="field-row">
        <div className="lbl">Read function code <span className="desc">Most H-100s answer 0x03</span></div>
        <select className="select" value={`0x0${v.read_fc}`} onChange={(e) => set({ read_fc: parseInt(e.target.value, 16) })}>
          <option value="0x03">0x03 — Read Holding Registers</option>
          <option value="0x04">0x04 — Read Input Registers</option>
        </select>
      </div>
      <div className="field-row">
        <div className="lbl">Prime poll interval <span className="desc">state &amp; alarms</span></div>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <input className="input" type="number" value={v.prime_poll_ms} onChange={(e) => set({ prime_poll_ms: Number(e.target.value) })} />
          <span className="mono" style={{ fontSize: 12, color: "var(--text-3)" }}>ms</span>
        </div>
      </div>
      <div className="field-row">
        <div className="lbl">Base poll interval <span className="desc">slow-changing telemetry</span></div>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <input className="input" type="number" value={v.base_poll_ms} onChange={(e) => set({ base_poll_ms: Number(e.target.value) })} />
          <span className="mono" style={{ fontSize: 12, color: "var(--text-3)" }}>ms</span>
        </div>
      </div>
    </div>
  );
}

function RegisterMapSection() {
  const [data, setData] = useState<Awaited<ReturnType<typeof api.registers>> | null>(null);
  const [reloading, setReloading] = useState(false);

  const refresh = async () => setData(await api.registers());
  useEffect(() => { refresh(); }, []);

  if (!data) return <div style={{ padding: 18, color: "var(--text-3)" }}>Loading…</div>;

  const grouped: Record<string, typeof data.registers> = {};
  for (const r of data.registers) (grouped[r.group] ||= []).push(r);

  const onReload = async () => {
    setReloading(true);
    try { await api.reloadRegisters(); await refresh(); } finally { setReloading(false); }
  };

  return (
    <Card title={`Register map — ${data.path.split("/").pop()}`}
          sub={`slave ${data.slave} · ${data.registers.length} registers`}
          actions={
            <button className="btn btn-ghost" disabled={reloading} onClick={onReload}>
              <Icon name="refresh" size={14} /> {reloading ? "…" : "Reload"}
            </button>
          }
          flush>
      <table className="reg-table">
        <thead>
          <tr><th>Address</th><th>Name</th><th>FC</th><th>Type</th><th>Scale</th><th>Unit</th><th>Last read</th></tr>
        </thead>
        <tbody>
          {Object.entries(grouped).map(([group, regs]) => (
            <>
              <tr key={group} className="group"><td colSpan={7}>{group}</td></tr>
              {regs.map((r) => (
                <tr key={r.addr + r.name}>
                  <td className="mono">{r.addr}</td>
                  <td className="mono" style={{ color: "var(--text)" }}>{r.name}</td>
                  <td className="mono">{r.fc}</td>
                  <td className="mono" style={{ color: "var(--text-3)" }}>{r.type}</td>
                  <td className="mono" style={{ color: "var(--text-3)" }}>{r.scale ?? "—"}</td>
                  <td>{r.unit}</td>
                  <td className="mono" style={{ color: "var(--text)" }}>
                    {r.value != null ? formatValue(r.value) : "—"}
                  </td>
                </tr>
              ))}
            </>
          ))}
        </tbody>
      </table>
    </Card>
  );
}

function formatValue(v: number): string {
  if (Number.isInteger(v)) return v.toLocaleString();
  return v.toFixed(2);
}

function RetentionSection({ v, set }: { v: Config["retention"]; set: (patch: Partial<Config["retention"]>) => void }) {
  return (
    <div className="settings-section">
      <div className="settings-head">
        <h2>Storage &amp; retention</h2>
        <p>SQLite in WAL mode. Aggregations run every 5 min.</p>
      </div>
      <div className="field-row">
        <div className="lbl">Raw telemetry <span className="desc">every base poll (~15 s)</span></div>
        <div className="flex ai-c gap-8">
          <input className="input" type="number" value={v.raw_days} onChange={(e) => set({ raw_days: Number(e.target.value) })} />
          <span className="mono" style={{ fontSize: 12, color: "var(--text-3)" }}>days</span>
        </div>
      </div>
      <div className="field-row">
        <div className="lbl">1-minute rollups</div>
        <div className="flex ai-c gap-8">
          <input className="input" type="number" value={v.rollup_1m_days} onChange={(e) => set({ rollup_1m_days: Number(e.target.value) })} />
          <span className="mono" style={{ fontSize: 12, color: "var(--text-3)" }}>days</span>
        </div>
      </div>
      <div className="field-row">
        <div className="lbl">1-hour rollups</div>
        <div className="flex ai-c gap-8">
          <input className="input" type="number" value={v.rollup_1h_days} onChange={(e) => set({ rollup_1h_days: Number(e.target.value) })} />
          <span className="mono" style={{ fontSize: 12, color: "var(--text-3)" }}>days</span>
        </div>
      </div>
    </div>
  );
}
