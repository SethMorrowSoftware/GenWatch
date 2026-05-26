// Settings view: modbus link (TCP bridge or USB serial), modbus protocol,
// register map, retention, alerts. Saves go through PUT /api/config
// (admin-only) and require a restart for link/modbus changes — we surface
// that warning rather than try to hot-reload the poller. Slack alert
// settings hot-reload immediately.

import { Fragment, useEffect, useState } from "react";
import { api } from "../api/client";
import { Card, Icon, Pill, Skeleton, Switch } from "../components/primitives";
import type { SlackConfigView, SlackUpdate } from "../types";

type Section = "link" | "modbus" | "registers" | "retention" | "alerts";
type Transport = "serial" | "tcp";

interface Config {
  configPath: string;
  mock: boolean;
  transport: Transport;
  serial: { device: string; baud: number; parity: string; stopbits: number; bytesize: number; timeout_s: number };
  modbus_tcp: { host: string; port: number; timeout_s: number; connect_timeout_s: number; framer: string };
  modbus: { slave: number; read_fc: number; prime_poll_ms: number; base_poll_ms: number; retries: number; register_file: string };
  retention: { raw_days: number; rollup_1m_days: number; rollup_1h_days: number; audit_days: number };
  auth: { operatorName: string; sessionHours: number; passwordConfigured: boolean; jwtSecretConfigured: boolean };
  slack: SlackConfigView;
}

export function SettingsView() {
  const [section, setSection] = useState<Section>("link");
  const [cfg, setCfg] = useState<Config | null>(null);
  const [dirty, setDirty] = useState<Partial<{ transport: Transport; serial: any; modbus_tcp: any; modbus: any; retention: any; slack: SlackUpdate }>>({});
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.config().then(setCfg).catch((e) => setError(e?.message ?? "failed to load config"));
  }, []);

  if (!cfg) return <SettingsLoadingSkeleton />;

  const effective = {
    transport: (dirty.transport ?? cfg.transport) as Transport,
    serial: { ...cfg.serial, ...(dirty.serial || {}) },
    modbus_tcp: { ...cfg.modbus_tcp, ...(dirty.modbus_tcp || {}) },
    modbus: { ...cfg.modbus, ...(dirty.modbus || {}) },
    retention: { ...cfg.retention, ...(dirty.retention || {}) },
    slack: { ...cfg.slack, ...(dirty.slack || {}) },
  };
  const hasDirty = Object.keys(dirty).length > 0;

  const save = async () => {
    setSaving(true);
    setError(null);
    setSaved(null);
    try {
      const r = await api.updateConfig(dirty as any);
      let message = "Saved.";
      if (r.restart_required) {
        message = r.slack_updated
          ? "Saved. Slack updated live · restart genwatch.service for link/modbus changes."
          : "Saved. Restart genwatch.service for changes to take effect.";
      } else if (r.slack_updated) {
        message = "Saved · Slack alerts updated live.";
      }
      setSaved(message);
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
    { id: "link", label: "Modbus Link", icon: "cable" },
    { id: "modbus", label: "Modbus", icon: "cpu" },
    { id: "registers", label: "Register Map", icon: "list" },
    { id: "retention", label: "Retention", icon: "history" },
    { id: "alerts", label: "Alerts · Slack", icon: "bell" },
  ];

  return (
    <>
      <div className="page-head">
        <div>
          <div className="page-eyebrow">Configuration</div>
          <h1 className="page-title">Settings</h1>
          <div className="page-sub">
            {cfg.mock ? <span style={{ color: "var(--amber)" }}>MOCK mode (no real link) · </span> : null}
            Transport <span className="mono">{effective.transport.toUpperCase()}</span>
            {effective.transport === "tcp"
              ? <> · <span className="mono">{effective.modbus_tcp.host}:{effective.modbus_tcp.port}</span></>
              : <> · <span className="mono">{effective.serial.device}</span></>}
            <> · </>
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
          {section === "link" && (
            <LinkSection
              transport={effective.transport}
              setTransport={(t) => setDirty((d) => ({ ...d, transport: t }))}
              serial={effective.serial}
              setSerial={(patch) => setDirty((d) => ({ ...d, serial: { ...(d.serial || {}), ...patch } }))}
              tcp={effective.modbus_tcp}
              setTcp={(patch) => setDirty((d) => ({ ...d, modbus_tcp: { ...(d.modbus_tcp || {}), ...patch } }))}
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
          {section === "alerts" && (
            <SlackSection
              v={effective.slack}
              dirty={dirty.slack ?? {}}
              set={(patch) => setDirty((d) => ({ ...d, slack: { ...(d.slack || {}), ...patch } }))}
            />
          )}
        </div>
      </div>
    </>
  );
}

function LinkSection({
  transport, setTransport,
  serial, setSerial,
  tcp, setTcp,
}: {
  transport: Transport;
  setTransport: (t: Transport) => void;
  serial: Config["serial"];
  setSerial: (patch: Partial<Config["serial"]>) => void;
  tcp: Config["modbus_tcp"];
  setTcp: (patch: Partial<Config["modbus_tcp"]>) => void;
}) {
  return (
    <div className="settings-section">
      <div className="settings-head">
        <h2>Modbus link</h2>
        <p>
          How this Pi reaches the H-100. Choose <b>TCP</b> for a network serial bridge
          (Lantronix UDS / EDS / xDirect, Moxa NPort, ser2net) or <b>Serial</b> for a direct
          USB-to-serial cable. Restart required after changes.
        </p>
      </div>
      <div className="field-row">
        <div className="lbl">Transport</div>
        <div style={{ display: "flex", gap: 8 }}>
          <button
            type="button"
            className={`btn ${transport === "tcp" ? "btn-primary" : "btn-ghost"}`}
            onClick={() => setTransport("tcp")}
          >
            <Icon name="cable" size={14} /> TCP bridge
          </button>
          <button
            type="button"
            className={`btn ${transport === "serial" ? "btn-primary" : "btn-ghost"}`}
            onClick={() => setTransport("serial")}
          >
            <Icon name="cable" size={14} /> USB serial
          </button>
        </div>
      </div>
      {transport === "tcp" ? <TcpFields v={tcp} set={setTcp} /> : <SerialFields v={serial} set={setSerial} />}
    </div>
  );
}

function TcpFields({ v, set }: { v: Config["modbus_tcp"]; set: (patch: Partial<Config["modbus_tcp"]>) => void }) {
  return (
    <>
      <div className="field-row">
        <div className="lbl">Host <span className="desc">Lantronix IP or hostname</span></div>
        <input className="input" value={v.host} onChange={(e) => set({ host: e.target.value })} />
      </div>
      <div className="field-row">
        <div className="lbl">TCP port <span className="desc">Lantronix Channel 1 raw-TCP default is 10001</span></div>
        <input className="input" type="number" value={v.port} onChange={(e) => set({ port: Number(e.target.value) })} />
      </div>
      <div className="field-row">
        <div className="lbl">Framer <span className="desc">Lantronix raw-TCP tunnels RTU bytes — use 'rtu' for the H-100</span></div>
        <select className="select" value={v.framer} onChange={(e) => set({ framer: e.target.value })}>
          <option value="rtu">rtu — Modbus RTU over TCP (Lantronix raw-socket bridge)</option>
          <option value="socket">socket — Modbus/TCP (MBAP header, no CRC; rare for H-100)</option>
        </select>
      </div>
      <div className="field-row">
        <div className="lbl">Request timeout <span className="desc">seconds; bump if LAN latency is high</span></div>
        <input className="input" type="number" step="0.1" value={v.timeout_s}
               onChange={(e) => set({ timeout_s: Number(e.target.value) })} />
      </div>
      <div className="field-row">
        <div className="lbl">Connect timeout <span className="desc">seconds; affects how fast boot fails when the bridge is unreachable</span></div>
        <input className="input" type="number" step="0.1" value={v.connect_timeout_s}
               onChange={(e) => set({ connect_timeout_s: Number(e.target.value) })} />
      </div>
    </>
  );
}

function SerialFields({ v, set }: { v: Config["serial"]; set: (patch: Partial<Config["serial"]>) => void }) {
  return (
    <>
      <div className="field-row">
        <div className="lbl">Device <span className="desc">/dev/genwatch-modbus, /dev/ttyUSB0, or /dev/serial0</span></div>
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
    </>
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
  const [verifying, setVerifying] = useState(false);
  const [verify, setVerify] = useState<Awaited<ReturnType<typeof api.verifyRegisters>> | null>(null);
  const [verifyErr, setVerifyErr] = useState<string | null>(null);

  const refresh = async () => setData(await api.registers());
  useEffect(() => { refresh(); }, []);

  if (!data) {
    return (
      <Card title="Register map" sub="loading…" flush>
        <div style={{ padding: "10px 0" }}>
          {Array.from({ length: 8 }).map((_, i) => (
            <div key={i} style={{ display: "grid", gridTemplateColumns: "80px 1fr 50px 80px 80px 80px 80px", gap: 12, padding: "10px 16px" }}>
              <Skeleton width="100%" height={12} />
              <Skeleton width="70%" height={12} />
              <Skeleton width="100%" height={12} />
              <Skeleton width="100%" height={12} />
              <Skeleton width="60%" height={12} />
              <Skeleton width="50%" height={12} />
              <Skeleton width="100%" height={12} />
            </div>
          ))}
        </div>
      </Card>
    );
  }

  const grouped: Record<string, typeof data.registers> = {};
  for (const r of data.registers) (grouped[r.group] ||= []).push(r);

  const onReload = async () => {
    setReloading(true);
    try { await api.reloadRegisters(); await refresh(); } finally { setReloading(false); }
  };

  const onVerify = async () => {
    setVerifying(true);
    setVerifyErr(null);
    try {
      setVerify(await api.verifyRegisters());
    } catch (e: any) {
      setVerifyErr(e?.body?.detail ?? e?.message ?? "verify failed");
    } finally {
      setVerifying(false);
    }
  };

  return (
    <Card title={`Register map — ${data.path.split("/").pop()}`}
          sub={`slave ${data.slave} · ${data.registers.length} registers`}
          actions={
            <>
              <button className="btn btn-ghost" disabled={reloading || verifying} onClick={onReload}>
                <Icon name="refresh" size={14} /> {reloading ? "…" : "Reload"}
              </button>
              <button className="btn btn-primary" disabled={reloading || verifying} onClick={onVerify}>
                <Icon name="check" size={14} /> {verifying ? "Verifying…" : "Verify map"}
              </button>
            </>
          }
          flush>
      {verifyErr && (
        <div style={{ padding: "10px 14px", borderBottom: "1px solid var(--border)", color: "var(--red)" }}>
          {verifyErr}
        </div>
      )}
      {verify && (
        <div style={{ padding: "10px 14px", borderBottom: "1px solid var(--border)", display: "grid", gap: 8 }}>
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            <Pill tone={verify.ok ? "ok" : "alarm"}>{verify.ok ? "Verification passed" : "Verification failed"}</Pill>
            <Pill tone={verify.static.ok ? "ok" : "alarm"}>
              Static: {verify.static.ok ? "OK" : `${verify.static.errors.length} errors`}
            </Pill>
            <Pill tone={verify.live.ok ? "ok" : "warn"}>
              Live: {verify.live.skipped ? "SKIPPED (mock mode)" : `${verify.live.tested - verify.live.failed}/${verify.live.tested} readable`}
            </Pill>
          </div>
          {verify.static.errors.length > 0 && (
            <div>
              <div className="mono" style={{ color: "var(--text-2)", marginBottom: 4 }}>Static errors</div>
              <ul style={{ margin: 0, paddingLeft: 18 }}>
                {verify.static.errors.map((e) => <li key={e} className="mono" style={{ color: "var(--red)" }}>{e}</li>)}
              </ul>
            </div>
          )}
          {verify.live.failures.length > 0 && (
            <div>
              <div className="mono" style={{ color: "var(--text-2)", marginBottom: 4 }}>Live read failures</div>
              <ul style={{ margin: 0, paddingLeft: 18 }}>
                {verify.live.failures.slice(0, 12).map((f) => (
                  <li key={`${f.name}-${f.addr}`} className="mono" style={{ color: "var(--amber)" }}>
                    {f.name} @{f.addr} fc={f.fc} → {f.error ?? "unknown"}
                  </li>
                ))}
              </ul>
              {verify.live.failures.length > 12 && (
                <div className="mono" style={{ color: "var(--text-3)", marginTop: 4 }}>
                  …and {verify.live.failures.length - 12} more
                </div>
              )}
            </div>
          )}
        </div>
      )}
      <table className="reg-table">
        <thead>
          <tr><th>Address</th><th>Name</th><th>FC</th><th>Type</th><th>Scale</th><th>Unit</th><th>Last read</th></tr>
        </thead>
        <tbody>
          {Object.entries(grouped).map(([group, regs]) => (
            <Fragment key={group}>
              <tr className="group"><td colSpan={7}>{group}</td></tr>
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
            </Fragment>
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

function SlackSection({
  v, dirty, set,
}: {
  v: SlackConfigView;
  dirty: SlackUpdate;
  set: (patch: SlackUpdate) => void;
}) {
  // Token UX: don't show the existing token (we don't have it client-side
  // anyway). Display "Configured" badge when the server reports one;
  // expose an input to change it. An empty string in the input + Save
  // explicitly clears it.
  const tokenWasSet = v.botTokenConfigured;
  const tokenDirty = dirty.bot_token !== undefined;
  const [revealing, setRevealing] = useState(!tokenWasSet);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<{ ok: boolean; detail: string } | null>(null);

  const runTest = async () => {
    setTesting(true);
    setTestResult(null);
    try {
      const r = await api.testSlack();
      setTestResult(r);
    } catch (e: any) {
      setTestResult({ ok: false, detail: e?.body?.detail ?? e?.message ?? "test failed" });
    } finally {
      setTesting(false);
    }
  };

  return (
    <div className="settings-section">
      <div className="settings-head">
        <h2>Slack alerts</h2>
        <p>
          Forward alarms, operator commands, and Modbus comms changes to a Slack channel via the
          Web API. Requires a Slack bot token (<span className="mono">xoxb-…</span>) with the{" "}
          <span className="mono">chat:write</span> scope and the bot invited to the target channel.
          Changes apply immediately — no restart required.
        </p>
      </div>

      <div className="field-row">
        <div className="lbl">Enabled <span className="desc">master switch</span></div>
        <Switch value={!!v.enabled} onChange={(b) => set({ enabled: b })} />
      </div>

      <div className="field-row">
        <div className="lbl">Channel <span className="desc">e.g. #generator-alerts or C0123ABCD</span></div>
        <input
          className="input"
          placeholder="#generator-alerts"
          value={v.channel}
          onChange={(e) => set({ channel: e.target.value })}
        />
      </div>

      <div className="field-row">
        <div className="lbl">Site label <span className="desc">overrides site.name in messages</span></div>
        <input
          className="input"
          placeholder={`(uses site name)`}
          value={v.siteLabel}
          onChange={(e) => set({ site_label: e.target.value })}
        />
      </div>

      <div className="field-row">
        <div className="lbl">
          Bot token <span className="desc">xoxb-… · stored on disk; never returned by API</span>
        </div>
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          {!revealing && tokenWasSet ? (
            <div className="flex ai-c gap-8">
              <Pill tone="ok">Configured</Pill>
              <button className="btn btn-ghost" onClick={() => { setRevealing(true); set({ bot_token: "" }); }}>
                Change…
              </button>
            </div>
          ) : (
            <>
              <input
                className="input mono"
                type="password"
                placeholder="xoxb-…"
                autoComplete="off"
                spellCheck={false}
                value={dirty.bot_token ?? ""}
                onChange={(e) => set({ bot_token: e.target.value })}
              />
              {tokenDirty && (
                <div style={{ fontSize: 11.5, color: "var(--text-3)" }}>
                  {dirty.bot_token === ""
                    ? "Save with an empty value to clear the token."
                    : "Token will be saved to /etc/genwatch/config.yaml."}
                </div>
              )}
              {tokenWasSet && (
                <button
                  className="btn btn-ghost"
                  style={{ alignSelf: "flex-start" }}
                  onClick={() => { setRevealing(false); set({ bot_token: undefined as any }); }}
                >
                  Keep existing token
                </button>
              )}
            </>
          )}
        </div>
      </div>

      <div className="settings-head" style={{ marginTop: 16, paddingTop: 14, borderTop: "1px solid var(--border)" }}>
        <h2 style={{ fontSize: 13 }}>Event types</h2>
        <p>Pick which events forward to Slack. State-change is off by default — it can be chatty.</p>
      </div>

      <SlackToggle label="Alarms"
        desc="Alarm-severity events (shutdowns, overspeed, overcrank…)"
        value={v.alertOnAlarm} onChange={(b) => set({ alert_on_alarm: b })} />
      <SlackToggle label="Warnings"
        desc="Warn-severity events (low battery, charger failure, …)"
        value={v.alertOnWarning} onChange={(b) => set({ alert_on_warning: b })} />
      <SlackToggle label="Alarm cleared"
        desc="Operator-ack and auto-clears"
        value={v.alertOnAlarmCleared} onChange={(b) => set({ alert_on_alarm_cleared: b })} />
      <SlackToggle label="Operator commands"
        desc="Start, stop, exercise, transfer"
        value={v.alertOnCommand} onChange={(b) => set({ alert_on_command: b })} />
      <SlackToggle label="Modbus comms"
        desc="Comms lost / recovered"
        value={v.alertOnCommsLost} onChange={(b) => set({ alert_on_comms_lost: b })} />
      <SlackToggle label="Load source"
        desc="Utility ↔ generator transitions (outage / restoration)"
        value={v.alertOnLoadSourceChange} onChange={(b) => set({ alert_on_load_source_change: b })} />
      <SlackToggle label="Engine state transitions"
        desc="Every stopped/cranking/running/cooling change — chatty"
        value={v.alertOnStateChange} onChange={(b) => set({ alert_on_state_change: b })} />

      <div className="field-row" style={{ marginTop: 14, paddingTop: 14, borderTop: "1px solid var(--border)" }}>
        <div className="lbl">Test message
          <span className="desc">posts a one-shot message to the configured channel</span></div>
        <div className="flex ai-c gap-8">
          <button
            className="btn"
            disabled={testing || !v.enabled || !v.channel || (!v.botTokenConfigured && !dirty.bot_token)}
            onClick={runTest}
          >
            {testing ? "Sending…" : "Send test"}
          </button>
          {testResult && (
            <Pill tone={testResult.ok ? "ok" : "alarm"}>
              {testResult.ok ? "Sent" : testResult.detail}
            </Pill>
          )}
        </div>
      </div>
      {!v.botTokenConfigured && !dirty.bot_token && (
        <div style={{
          marginTop: 8, padding: 10, borderRadius: 7, fontSize: 12,
          background: "var(--panel-2)", border: "1px solid var(--border)", color: "var(--text-3)",
        }}>
          Save the bot token first, then the Send test button enables.
        </div>
      )}
    </div>
  );
}

function SlackToggle({
  label, desc, value, onChange,
}: {
  label: string; desc: string; value: boolean; onChange: (b: boolean) => void;
}) {
  return (
    <div className="field-row">
      <div className="lbl">{label} <span className="desc">{desc}</span></div>
      <Switch value={!!value} onChange={onChange} />
    </div>
  );
}

function SettingsLoadingSkeleton() {
  return (
    <>
      <div className="page-head">
        <div>
          <Skeleton width={140} height={22} />
          <div style={{ marginTop: 6 }}><Skeleton width={280} height={13} /></div>
        </div>
      </div>
      <div className="settings-grid">
        <nav className="settings-side">
          {Array.from({ length: 5 }).map((_, i) => (
            <div key={i} style={{ padding: "9px 12px" }}><Skeleton width="80%" height={14} /></div>
          ))}
        </nav>
        <div className="settings-section">
          <div className="settings-head">
            <Skeleton width={120} height={16} />
            <div style={{ marginTop: 8 }}><Skeleton width="80%" height={13} /></div>
          </div>
          {Array.from({ length: 4 }).map((_, i) => (
            <div key={i} className="field-row">
              <Skeleton width={140} height={14} />
              <Skeleton width="100%" height={34} radius={8} />
            </div>
          ))}
        </div>
      </div>
    </>
  );
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
