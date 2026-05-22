// Shared visual components: cards, sparklines, gauges, modal, icons.

const Icon = ({ name, size = 16, stroke = 1.6 }) => {
  const p = (d) => <path d={d} fill="none" stroke="currentColor" strokeWidth={stroke} strokeLinecap="round" strokeLinejoin="round" />;
  const c = (cx, cy, r) => <circle cx={cx} cy={cy} r={r} fill="none" stroke="currentColor" strokeWidth={stroke} />;
  const paths = {
    activity:  <>{p("M3 12h4l3-8 4 16 3-8h4")}</>,
    bolt:      <>{p("M13 2 4 14h7l-1 8 9-12h-7z")}</>,
    gauge:     <>{c(12,12,9)}{p("M12 12 16 8")}</>,
    bell:      <>{p("M6 8a6 6 0 1 1 12 0v4l2 3H4l2-3z")}{p("M10 19a2 2 0 0 0 4 0")}</>,
    history:   <>{p("M3 12a9 9 0 1 0 3-6.7")}{p("M3 4v5h5")}{p("M12 7v5l3 2")}</>,
    settings:  <>{c(12,12,3)}{p("M12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9l2.1 2.1M17 17l2.1 2.1M4.9 19.1 7 17M17 7l2.1-2.1")}</>,
    play:      <>{p("M6 4v16l14-8z")}</>,
    stop:      <>{p("M5 5h14v14H5z")}</>,
    refresh:   <>{p("M20 12a8 8 0 1 1-2.34-5.66")}{p("M20 4v5h-5")}</>,
    chevron:   <>{p("M9 6l6 6-6 6")}</>,
    check:     <>{p("M4 12 10 18 20 6")}</>,
    x:         <>{p("M5 5 19 19M19 5 5 19")}</>,
    arrow:     <>{p("M4 12h16M14 6l6 6-6 6")}</>,
    switch_:   <>{p("M3 8h12a4 4 0 1 1 0 8H3")}{c(15,12,2)}</>,
    lock:      <>{p("M6 11V8a6 6 0 1 1 12 0v3")}{p("M5 11h14v10H5z")}</>,
    user:      <>{c(12,8,4)}{p("M4 21c0-4 4-7 8-7s8 3 8 7")}</>,
    filter:    <>{p("M3 5h18M6 12h12M10 19h4")}</>,
    download:  <>{p("M12 4v12M6 10l6 6 6-6M4 20h16")}</>,
    plus:      <>{p("M12 5v14M5 12h14")}</>,
    flame:     <>{p("M12 3s4 4 4 9a4 4 0 0 1-8 0c0-2 1-3 2-4 0 2 1 3 2 3-1-2-2-5 0-8z")}</>,
    drop:      <>{p("M12 3s6 7 6 12a6 6 0 1 1-12 0c0-5 6-12 6-12z")}</>,
    spark:     <>{p("M3 17l5-7 4 4 4-6 5 5")}</>,
    wave:      <>{p("M3 12c2-4 4-4 6 0s4 4 6 0 4-4 6 0")}</>,
    cable:     <>{p("M4 10v2a4 4 0 0 0 4 4v0a4 4 0 0 0 4-4v-4a4 4 0 0 1 4-4v0a4 4 0 0 1 4 4v2")}</>,
    cpu:       <>{p("M6 6h12v12H6z")}{p("M9 9h6v6H9z")}{p("M3 9h3M3 15h3M18 9h3M18 15h3M9 3v3M15 3v3M9 18v3M15 18v3")}</>,
    bookmark:  <>{p("M6 4h12v18l-6-4-6 4z")}</>,
    folder:    <>{p("M3 7l3-3h5l2 3h8v11H3z")}</>,
    list:      <>{p("M4 6h16M4 12h16M4 18h16")}</>,
    plug:      <>{p("M9 7V3M15 7V3M6 7h12v4a6 6 0 1 1-12 0V7zM12 17v4")}</>,
    bars:      <>{p("M4 4v16M4 20h16")}{p("M9 14v4M14 10v8M19 6v12")}</>,
  };
  return <svg width={size} height={size} viewBox="0 0 24 24" aria-hidden="true">{paths[name]}</svg>;
};

// ─── Pill ───────────────────────────────────────────────────────────────────
const Pill = ({ tone, children }) => (
  <span className="pill" data-tone={tone}><i className="d" />{children}</span>
);

// ─── Sparkline (path + filled area) ─────────────────────────────────────────
const Sparkline = ({ points, width = 120, height = 36, color = "currentColor", fill = true, strokeWidth = 1.5, smooth = true }) => {
  if (!points || points.length < 2) return <svg width={width} height={height} />;
  const min = Math.min(...points);
  const max = Math.max(...points);
  const range = max - min || 1;
  const pad = 2;
  const innerH = height - pad * 2;
  const innerW = width - pad * 2;
  const xs = points.map((_, i) => pad + (i / (points.length - 1)) * innerW);
  const ys = points.map(p => pad + innerH - ((p - min) / range) * innerH);
  let d = `M ${xs[0].toFixed(2)} ${ys[0].toFixed(2)}`;
  for (let i = 1; i < points.length; i++) {
    if (smooth) {
      const cx = (xs[i - 1] + xs[i]) / 2;
      d += ` Q ${cx.toFixed(2)} ${ys[i - 1].toFixed(2)} ${cx.toFixed(2)} ${((ys[i - 1] + ys[i]) / 2).toFixed(2)}`;
      d += ` T ${xs[i].toFixed(2)} ${ys[i].toFixed(2)}`;
    } else {
      d += ` L ${xs[i].toFixed(2)} ${ys[i].toFixed(2)}`;
    }
  }
  const area = `${d} L ${xs[xs.length - 1].toFixed(2)} ${(pad + innerH).toFixed(2)} L ${xs[0].toFixed(2)} ${(pad + innerH).toFixed(2)} Z`;
  const gid = `g-${color.replace(/[^a-z0-9]/gi, "")}-${width}`;
  return (
    <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`}>
      <defs>
        <linearGradient id={gid} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%"  stopColor={color} stopOpacity="0.28" />
          <stop offset="100%" stopColor={color} stopOpacity="0" />
        </linearGradient>
      </defs>
      {fill && <path d={area} fill={`url(#${gid})`} className="spark-fill" />}
      <path d={d} fill="none" stroke={color} strokeWidth={strokeWidth} strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
};

// ─── Big line chart for history ─────────────────────────────────────────────
const LineChart = ({ series, width = 800, height = 280, colors = ["var(--green)", "var(--amber)", "var(--blue)"], xLabels = [] }) => {
  const padL = 44, padR = 16, padT = 14, padB = 28;
  const innerW = width - padL - padR;
  const innerH = height - padT - padB;
  const all = series.flatMap(s => s.data);
  const min = Math.min(...all);
  const max = Math.max(...all);
  const range = (max - min) * 1.1 || 1;
  const lo = min - (max - min) * 0.05;
  const yTicks = 5;
  const tickVals = Array.from({length: yTicks}, (_, i) => lo + (range / (yTicks - 1)) * i);
  return (
    <svg width="100%" viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" style={{display:'block'}}>
      {tickVals.map((v, i) => {
        const y = padT + innerH - ((v - lo) / range) * innerH;
        return (
          <g key={i}>
            <line x1={padL} x2={width - padR} y1={y} y2={y} stroke="var(--border)" strokeDasharray={i === 0 ? "" : "2 3"} />
            <text x={padL - 8} y={y + 3} fill="var(--text-3)" fontSize="10" fontFamily="JetBrains Mono" textAnchor="end">{Math.round(v).toLocaleString()}</text>
          </g>
        );
      })}
      {xLabels.map((lbl, i) => {
        const x = padL + (i / Math.max(1, xLabels.length - 1)) * innerW;
        return <text key={i} x={x} y={height - 8} fill="var(--text-3)" fontSize="10" fontFamily="JetBrains Mono" textAnchor="middle">{lbl}</text>;
      })}
      {series.map((s, si) => {
        const n = s.data.length;
        const xs = s.data.map((_, i) => padL + (i / (n - 1)) * innerW);
        const ys = s.data.map(v => padT + innerH - ((v - lo) / range) * innerH);
        let d = `M ${xs[0]} ${ys[0]}`;
        for (let i = 1; i < n; i++) {
          const cx = (xs[i - 1] + xs[i]) / 2;
          d += ` Q ${cx} ${ys[i - 1]} ${cx} ${(ys[i - 1] + ys[i]) / 2}`;
          d += ` T ${xs[i]} ${ys[i]}`;
        }
        const area = `${d} L ${xs[n-1]} ${padT + innerH} L ${xs[0]} ${padT + innerH} Z`;
        const gid = `gc-${si}`;
        return (
          <g key={si}>
            <defs>
              <linearGradient id={gid} x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%"  stopColor={colors[si % colors.length]} stopOpacity="0.18" />
                <stop offset="100%" stopColor={colors[si % colors.length]} stopOpacity="0" />
              </linearGradient>
            </defs>
            <path d={area} fill={`url(#${gid})`} />
            <path d={d} fill="none" stroke={colors[si % colors.length]} strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
          </g>
        );
      })}
    </svg>
  );
};

// ─── Radial gauge ────────────────────────────────────────────────────────────
const Gauge = ({ value, min = 0, max = 100, color = "var(--accent)", size = 92, strokeWidth = 9 }) => {
  const r = (size - strokeWidth) / 2;
  const c2 = size / 2;
  const startAngle = 135;     // degrees
  const endAngle = 405;       // 270° sweep
  const sweep = endAngle - startAngle;
  const pct = Math.max(0, Math.min(1, (value - min) / (max - min)));
  const toXY = (deg) => {
    const r2 = r;
    const a = (deg * Math.PI) / 180;
    return [c2 + r2 * Math.cos(a), c2 + r2 * Math.sin(a)];
  };
  const arc = (a0, a1) => {
    const [x0, y0] = toXY(a0);
    const [x1, y1] = toXY(a1);
    const large = a1 - a0 > 180 ? 1 : 0;
    return `M ${x0} ${y0} A ${r} ${r} 0 ${large} 1 ${x1} ${y1}`;
  };
  return (
    <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`}>
      <path d={arc(startAngle, endAngle)} stroke="var(--panel-3)" strokeWidth={strokeWidth} fill="none" strokeLinecap="round" />
      <path d={arc(startAngle, startAngle + sweep * pct)} stroke={color} strokeWidth={strokeWidth} fill="none" strokeLinecap="round" />
      {/* tick marks */}
      {[0, 0.25, 0.5, 0.75, 1].map((t, i) => {
        const a = (startAngle + sweep * t) * Math.PI / 180;
        const r0 = r - strokeWidth/2 - 2;
        const r1 = r - strokeWidth/2 - 6;
        return <line key={i} x1={c2 + r0 * Math.cos(a)} y1={c2 + r0 * Math.sin(a)}
                     x2={c2 + r1 * Math.cos(a)} y2={c2 + r1 * Math.sin(a)} stroke="var(--text-4)" strokeWidth="1" />;
      })}
    </svg>
  );
};

// ─── Card ────────────────────────────────────────────────────────────────────
const Card = ({ title, sub, actions, children, className = "", flush = false, tight = false }) => (
  <div className={`card ${className}`}>
    {(title || actions) && (
      <div className="card-head">
        <div>
          <h3>{title}</h3>
          {sub && <div style={{ fontSize: 11, color: "var(--text-3)", marginTop: 2, textTransform: "none", letterSpacing: 0, whiteSpace: "nowrap" }}>{sub}</div>}
        </div>
        {actions && <div className="actions">{actions}</div>}
      </div>
    )}
    <div className={`card-body ${flush ? "flush" : ""} ${tight ? "tight" : ""}`}>{children}</div>
  </div>
);

// ─── Modal ───────────────────────────────────────────────────────────────────
const Modal = ({ open, onClose, title, sub, children, footer }) => {
  if (!open) return null;
  return (
    <div className="modal-scrim" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()} role="dialog" aria-modal="true">
        <div className="modal-head">
          <h2>{title}</h2>
          {sub && <p>{sub}</p>}
        </div>
        <div className="modal-body">{children}</div>
        {footer && <div className="modal-foot">{footer}</div>}
      </div>
    </div>
  );
};

// ─── Live tick indicator ─────────────────────────────────────────────────────
const LiveTick = ({ rateMs = 1500 }) => (
  <span className="live-tick"><i className="d" />LIVE · {(rateMs/1000).toFixed(1)}s</span>
);

// ─── Switch (settings toggle) ────────────────────────────────────────────────
const Switch = ({ value, onChange }) => (
  <button className="switch" data-on={value ? "1" : "0"} onClick={() => onChange(!value)} aria-pressed={!!value} />
);

Object.assign(window, { Icon, Pill, Sparkline, LineChart, Gauge, Card, Modal, LiveTick, Switch });
