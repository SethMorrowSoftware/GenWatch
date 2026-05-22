// Synthetic telemetry, event log, state machine.
// No real Modbus — this is the prototype shape.

const STATES = {
  stopped:    { label: "Stopped",    sub: "AUTO · Ready",          rpm: 0,    fHz: 0,    kw: 0,   oilP: 0,   coolT: 90 },
  cranking:   { label: "Cranking",   sub: "Engine start in progress", rpm: 400,  fHz: 0,   kw: 0,   oilP: 18,  coolT: 92 },
  running:    { label: "Running",    sub: "On load · Utility lost", rpm: 1800, fHz: 60.0, kw: 142, oilP: 64,  coolT: 188 },
  exercising: { label: "Exercising", sub: "Quiet-Test · No load",   rpm: 1798, fHz: 60.0, kw: 6,   oilP: 62,  coolT: 174 },
  cooling:    { label: "Cooling",    sub: "Engine cool-down · 5:00 left", rpm: 1500, fHz: 60.0, kw: 0,  oilP: 58, coolT: 196 },
  alarm:      { label: "Alarm",      sub: "Shutdown · Operator action required", rpm: 0, fHz: 0, kw: 0, oilP: 0,  coolT: 165 },
};

function jitter(base, amp, seed) {
  // Smooth deterministic wobble — multi-octave at low frequency so charts
  // read as plausible analog telemetry instead of pixel hash.
  const a = Math.sin(seed * 0.062) * 0.55;
  const b = Math.sin(seed * 0.181 + 1.3) * 0.30;
  const c = Math.sin(seed * 0.043 + 2.7) * 0.15;
  return base + (a + b + c) * amp;
}

function buildSeries(state, n, seed0) {
  const s = STATES[state] || STATES.stopped;
  const out = [];
  for (let i = 0; i < n; i++) {
    const t = seed0 + i;
    out.push({
      rpm:    s.rpm   ? Math.max(0, jitter(s.rpm,   8, t))     : 0,
      hz:     s.fHz   ? Math.max(0, jitter(s.fHz,   0.08, t))  : 0,
      kw:     s.kw    ? Math.max(0, jitter(s.kw,    4, t * 1.3)) : 0,
      oilP:   s.oilP  ? Math.max(0, jitter(s.oilP,  1.2, t * 0.8)) : 0,
      coolT:  Math.max(50, jitter(s.coolT, 1.5, t * 0.5)),
      batt:   jitter(state === "stopped" ? 12.6 : 13.9, 0.05, t * 0.3),
      vAB:    s.rpm ? jitter(480, 1.2, t * 1.1) : 0,
      vBC:    s.rpm ? jitter(481, 1.1, t * 1.2) : 0,
      vCA:    s.rpm ? jitter(479, 1.0, t * 1.0) : 0,
      iA:     s.rpm ? jitter(state === "exercising" ? 8 : 172, 3, t * 0.9) : 0,
      iB:     s.rpm ? jitter(state === "exercising" ? 7 : 168, 3, t * 0.95) : 0,
      iC:     s.rpm ? jitter(state === "exercising" ? 9 : 176, 3, t * 1.05) : 0,
      fuelPct: 78 - i * 0.005,
    });
  }
  return out;
}

function currentReading(state, seed) {
  return buildSeries(state, 1, seed)[0];
}

const EVENTS = [
  { t: "—00:00:02", sev: "info",  type: "TELEMETRY",   msg: "Comms-health restored — 100% success",              meta: "0/0 err" },
  { t: "—00:00:43", sev: "warn",  type: "COMMS",       msg: "CRC mismatch on FC03 — recovered",                  meta: "addr 0x0010" },
  { t: "—00:02:18", sev: "ok",    type: "TRANSITION",  msg: "Engine state: <em>Cranking</em> → <em>Running</em>", meta: "12.8 s" },
  { t: "—00:02:30", sev: "info",  type: "TRANSFER",    msg: "HTS-1 transferred to <em>Generator</em>",            meta: "0.18 s" },
  { t: "—00:02:31", sev: "ok",    type: "COMMAND",     msg: "Operator command <em>Remote Start</em> — confirmed", meta: "kim.harris" },
  { t: "—00:03:04", sev: "warn",  type: "UTILITY",     msg: "Utility voltage out of band — phase B at 102.4 V",   meta: "—" },
  { t: "—00:08:51", sev: "info",  type: "EXERCISE",    msg: "Quiet-Test scheduled — Sun 03:00, 30 min, no load",  meta: "next 4d 11h" },
  { t: "—00:11:22", sev: "alarm", type: "ALARM",       msg: "Alarm cleared — <em>Low Coolant Level</em> (cleared by operator)", meta: "code 0x52" },
  { t: "—00:18:09", sev: "info",  type: "CONFIG",      msg: "Register file reloaded — h100.yaml (rev 14)",        meta: "—" },
  { t: "—00:23:44", sev: "ok",    type: "SNAPSHOT",    msg: "Daily summary persisted — 142 events, 2 alarms",     meta: "84.1 MB" },
  { t: "—01:02:17", sev: "warn",  type: "BATTERY",     msg: "Battery 12.42 V at engine-off — below warn 12.6 V",  meta: "—" },
  { t: "—01:14:50", sev: "alarm", type: "ALARM",       msg: "Alarm raised — <em>Low Coolant Level</em> (sensor #3)", meta: "code 0x52" },
  { t: "—02:00:00", sev: "ok",    type: "EXERCISE",    msg: "Quiet-Test complete — 30:00 runtime · 0 alarms",      meta: "0 kWh" },
  { t: "—02:30:00", sev: "ok",    type: "TRANSITION",  msg: "Engine state: <em>Stopped</em> → <em>Cranking</em>",  meta: "scheduled" },
  { t: "—04:18:11", sev: "info",  type: "TELEMETRY",   msg: "Retention job — pruned 12,418 raw points (>7d)",     meta: "—" },
  { t: "—08:01:55", sev: "ok",    type: "AUTH",        msg: "User <em>kim.harris</em> signed in from 100.74.x.x",  meta: "session" },
];

const ACTIVE_ALARMS = [
  { code: "0x52", desc: "Low Coolant Level", sev: "alarm", since: "00:14:23" },
];

const REGISTERS = [
  { group: "Prime poll · 1.5 s" },
  { addr: "0x0001", name: "engine_state",     fc: "03", type: "u16", unit: "enum",  value: "RUNNING (3)" },
  { addr: "0x0002", name: "alarm_state",      fc: "03", type: "u16", unit: "enum",  value: "OK (0)" },
  { addr: "0x0003", name: "switch_state",     fc: "03", type: "u16", unit: "bitfld", value: "0b0000_0011" },
  { group: "Base poll · 15 s" },
  { addr: "0x0010", name: "rpm",              fc: "03", type: "u16", unit: "rpm",   value: "1,798" },
  { addr: "0x0011", name: "oil_pressure",     fc: "03", type: "u16", unit: "psi",   value: "64.0" },
  { addr: "0x0012", name: "coolant_temp",     fc: "03", type: "u16", unit: "°F",    value: "188" },
  { addr: "0x0013", name: "battery_volts",    fc: "03", type: "u16", unit: "V",     value: "13.92", scale: "×0.1" },
  { addr: "0x0020", name: "gen_voltage_ab",   fc: "03", type: "u16", unit: "V",     value: "480" },
  { addr: "0x0023", name: "gen_current_a",    fc: "03", type: "u16", unit: "A",     value: "172" },
  { addr: "0x0026", name: "frequency",        fc: "03", type: "u16", unit: "Hz",    value: "60.0", scale: "×0.1" },
  { addr: "0x0028", name: "total_kw",         fc: "03", type: "u16", unit: "kW",    value: "142" },
  { addr: "0x0030", name: "run_hours",        fc: "03", type: "u32", unit: "h",     value: "1,847.6", scale: "×0.1" },
  { group: "Controls · write-gated" },
  { addr: "0x00A0", name: "remote_start",     fc: "06", type: "u16", unit: "cmd",   value: "—" },
  { addr: "0x00A1", name: "remote_stop",      fc: "06", type: "u16", unit: "cmd",   value: "—" },
  { addr: "0x00A2", name: "exercise",         fc: "06", type: "u16", unit: "cmd",   value: "—" },
];

const ALARM_CODES = [
  { code: "0x40", desc: "Overcrank",              sev: "alarm" },
  { code: "0x41", desc: "Overspeed",              sev: "alarm" },
  { code: "0x42", desc: "Low Oil Pressure",       sev: "alarm" },
  { code: "0x43", desc: "High Engine Temp",       sev: "alarm" },
  { code: "0x44", desc: "Emergency Stop",         sev: "alarm" },
  { code: "0x52", desc: "Low Coolant Level",      sev: "alarm" },
  { code: "0x60", desc: "Low Battery",            sev: "warn"  },
  { code: "0x61", desc: "Charger Failure",        sev: "warn"  },
  { code: "0x70", desc: "Comms Loss (RS-485)",    sev: "warn"  },
  { code: "0x71", desc: "HTS-1 Not Responding",   sev: "warn"  },
];

Object.assign(window, { STATES, buildSeries, currentReading, EVENTS, ACTIVE_ALARMS, REGISTERS, ALARM_CODES });
