// Two-step confirm modal — same UX as the design prototype, wired to
// the real /api/control/confirm + /api/control/<verb> flow.

import { useEffect, useState } from "react";
import { api, ApiError } from "../api/client";
import { Icon, Modal } from "../components/primitives";

type Verb = "start" | "stop" | "exercise" | "transfer";

const SPECS: Record<Verb, { title: string; verb: string; danger: boolean; bullets: string[] }> = {
  start: {
    title: "Confirm Remote Start", verb: "Start", danger: false,
    bullets: [
      "Engine will crank within 2 seconds",
      "HTS-1 stays on UTILITY (no load transfer)",
      "Run hours will accumulate",
    ],
  },
  stop: {
    title: "Confirm Remote Stop", verb: "Stop", danger: true,
    bullets: [
      "HTS-1 will transfer back to UTILITY",
      "Engine enters 5-minute cool-down",
      "Site briefly on utility-only",
    ],
  },
  exercise: {
    title: "Confirm Quiet-Test", verb: "Start exercise", danger: false,
    bullets: [
      "Engine runs unloaded for 30:00",
      "No transfer · utility remains primary",
      "Sound profile: quiet mode",
    ],
  },
  transfer: {
    title: "Confirm Transfer Back", verb: "Transfer", danger: false,
    bullets: [
      "HTS-1 → UTILITY",
      "Engine continues running through cool-down",
      "Brief 100-200 ms power gap on load",
    ],
  },
};

interface Props {
  command: Verb | null;
  operator: string;
  onClose: () => void;
  onSuccess: () => void;
}

export function ConfirmModal({ command, operator, onClose, onSuccess }: Props) {
  const [token, setToken] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!command) return;
    setToken("");
    setConfirmed(false);
    setError(null);
    let cancelled = false;
    api.confirmToken()
      .then((r) => { if (!cancelled) setToken(r.token); })
      .catch((e: ApiError) => {
        if (cancelled) return;
        setError(e.status === 401 ? "Session expired — sign in again" : "Failed to fetch confirm token");
      });
    return () => { cancelled = true; };
  }, [command]);

  if (!command) return null;
  const spec = SPECS[command];

  const submit = async () => {
    if (!token || !confirmed || submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      await api.control(command, token);
      onSuccess();
    } catch (e: any) {
      const detail = e?.body?.detail;
      const msg =
        detail?.message ??
        (typeof detail === "string" ? detail : null) ??
        e?.message ??
        "Control command failed";
      setError(msg);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Modal
      open
      onClose={onClose}
      title={spec.title}
      sub={`Two-step confirm · audit-logged · operator: ${operator}`}
      footer={
        <>
          <button className="btn btn-ghost" onClick={onClose}>Cancel</button>
          <button
            className={spec.danger ? "btn btn-danger" : "btn btn-primary"}
            disabled={!confirmed || !token || submitting}
            onClick={submit}
          >
            <Icon name="check" size={14} /> {submitting ? "Working…" : spec.verb}
          </button>
        </>
      }
    >
      <div style={{ marginBottom: 14 }}>
        {spec.bullets.map((b, i) => (
          <div key={i} className="check-line on">
            <span className="cb"><Icon name="check" size={12} stroke={2.6} /></span>
            <div><div className="lbl">{b}</div></div>
          </div>
        ))}
      </div>
      <div className="check-line" style={{ cursor: "pointer" }} onClick={() => setConfirmed((c) => !c)}>
        <span className="cb" style={confirmed ? { borderColor: "var(--green)", background: "var(--green)", color: "var(--bg)" } : undefined}>
          {confirmed && <Icon name="check" size={12} stroke={2.6} />}
        </span>
        <div>
          <div className="lbl">I understand this will physically affect the generator and load.</div>
          <div className="desc">Hardware safeties at the H-100 panel remain primary.</div>
        </div>
      </div>
      <div style={{
        marginTop: 12, padding: 10, background: "var(--panel-2)", borderRadius: 7,
        border: "1px solid var(--border)", display: "flex",
        justifyContent: "space-between", alignItems: "center", fontSize: 12,
      }}>
        <span className="text-fa">Confirm token (valid 30 s)</span>
        <span className="mono" style={{ color: "var(--text)", fontSize: 13 }}>{token || "…"}</span>
      </div>
      {error && (
        <div style={{
          marginTop: 10, padding: 10, background: "color-mix(in oklch, var(--red) 10%, var(--panel-2))",
          color: "var(--red)", borderRadius: 7, fontSize: 12.5,
          border: "1px solid color-mix(in oklch, var(--red) 35%, var(--border))",
        }}>
          {error}
        </div>
      )}
    </Modal>
  );
}
