// Two-step confirm modal — same UX as the design prototype, wired to
// the real /api/control/confirm + /api/control/<verb> flow.

import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "../api/client";
import { Icon, Modal } from "../components/primitives";

type Verb = "start" | "stop" | "exercise" | "transfer";

// Discriminated command the modal can confirm. H-100 verbs carry no
// extra args; ATS maintained commands carry assert (assert vs release)
// and force-transfer additionally carries override (set by the caller
// when utility is available, so the backend's healthy-utility guard is
// satisfied after the operator confirms the warning copy).
export type ConfirmCmd =
  | { kind: Verb }
  | { kind: "ats_test" }
  | { kind: "ats_inhibit"; assert: boolean }
  | { kind: "ats_force_transfer"; assert: boolean; override: boolean }
  | { kind: "ats_bypass_delay" };

interface Spec {
  title: string;
  verb: string;
  danger: boolean;
  bullets: string[];
}

const H100_SPECS: Record<Verb, Spec> = {
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
    // The underlying Modbus command (`starttransfer`) asserts the
    // H-100's transfer-signal output, which closes the ATS onto the
    // generator. This moves load FROM utility TO generator — the
    // opposite of what an earlier version of this modal claimed.
    title: "Confirm Transfer to Generator", verb: "Transfer", danger: true,
    bullets: [
      "HTS-1 → GENERATOR (load moves onto generator)",
      "Engine continues running carrying load",
      "Brief 100-200 ms power gap on load during the transfer",
      "Use Remote Stop to retransfer back to utility",
    ],
  },
};

function specFor(cmd: ConfirmCmd): Spec {
  switch (cmd.kind) {
    case "start":
    case "stop":
    case "exercise":
    case "transfer":
      return H100_SPECS[cmd.kind];
    case "ats_test":
      return {
        title: "Confirm ATS Test", verb: "Test transfer", danger: false,
        bullets: [
          "Pulses the ATS test input (ASCO terminals 6–7)",
          "The ATS performs a live test transfer to the generator",
          "Momentary — self-clears within ~1.5 s",
        ],
      };
    case "ats_inhibit":
      return cmd.assert
        ? {
            title: "Confirm Inhibit Transfer", verb: "Inhibit", danger: true,
            bullets: [
              "Asserts the ASCO inhibit input (maintained)",
              "The ATS will NOT transfer to the generator while inhibited",
              "Auto-releases ~30 s after a GenWatch comms loss (ICD §8.3)",
            ],
          }
        : {
            title: "Release Inhibit", verb: "Release inhibit", danger: false,
            bullets: [
              "Clears the inhibit signal",
              "The ATS resumes normal automatic transfer logic",
            ],
          };
    case "ats_force_transfer":
      if (!cmd.assert) {
        return {
          title: "Release Force-Transfer", verb: "Release force", danger: false,
          bullets: [
            "Clears the force-transfer signal",
            "The ATS resumes normal automatic logic",
          ],
        };
      }
      return {
        title: "Confirm FORCE TRANSFER", verb: "Force transfer", danger: true,
        bullets: [
          ...(cmd.override
            ? ["⚠ Utility (normal source) is AVAILABLE — this drops a healthy utility feed and moves the load onto the generator."]
            : []),
          "Asserts the ASCO force-transfer input (maintained)",
          "Load transfers to the generator",
          "Admin-only · auto-releases ~30 s after a GenWatch comms loss (ICD §8.3)",
        ],
      };
    case "ats_bypass_delay":
      return {
        title: "Confirm Bypass Delay", verb: "Bypass delay", danger: false,
        bullets: [
          "Pulses the ASCO bypass-transfer-time-delay input",
          "Skips the ATS's transfer time delay for the next transfer",
          "Momentary — self-clears within ~1.5 s",
        ],
      };
  }
}

/** Backend verb a confirm token is bound to for this command. */
function verbFor(cmd: ConfirmCmd): string {
  switch (cmd.kind) {
    case "start":
    case "stop":
    case "exercise":
    case "transfer":
      return cmd.kind;
    case "ats_test":
      return "test";
    case "ats_inhibit":
      return "inhibit";
    case "ats_force_transfer":
      return "force_transfer";
    case "ats_bypass_delay":
      return "bypass_delay";
  }
}

async function runCommand(cmd: ConfirmCmd, token: string): Promise<unknown> {
  switch (cmd.kind) {
    case "start":
    case "stop":
    case "exercise":
    case "transfer":
      return api.control(cmd.kind, token);
    case "ats_test":
      return api.atsCommand("test", token);
    case "ats_inhibit":
      return api.atsCommand("inhibit", token, { assert: cmd.assert });
    case "ats_force_transfer":
      return api.atsCommand("force_transfer", token, { assert: cmd.assert, override: cmd.override });
    case "ats_bypass_delay":
      return api.atsCommand("bypass_delay", token);
  }
}

interface Props {
  command: ConfirmCmd | null;
  operator: string;
  onClose: () => void;
  onSuccess: () => void;
}

export function ConfirmModal({ command, operator, onClose, onSuccess }: Props) {
  const [token, setToken] = useState("");
  // Wall-clock instant (ms since epoch) at which the current token expires.
  // 0 means "no live token yet" — the modal renders "…" in that state.
  const [tokenExpiresAtMs, setTokenExpiresAtMs] = useState(0);
  // 1Hz tick driving the countdown render. Stored separately from the
  // token state so a re-fetch doesn't have to re-trigger the interval.
  const [nowMs, setNowMs] = useState(Date.now());
  const [confirmed, setConfirmed] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Set to true on a failed token fetch so the auto-refresh effect
  // doesn't pin the API in a loop. Cleared on every modal-open.
  const [fetchFailed, setFetchFailed] = useState(false);

  // useRef-backed fetcher so the auto-refresh effect can depend on a
  // stable identity (we want to refetch on countdown==0, not on every
  // re-render of the component).
  const fetchToken = useCallback(() => {
    setToken("");
    setTokenExpiresAtMs(0);
    setError(null);
    setFetchFailed(false);
    api.confirmToken(command ? verbFor(command) : undefined)
      .then((r) => {
        setToken(r.token);
        // Anchor expiry to OUR clock at receipt, not the server's
        // absolute unix timestamp. The Pi runs without an RTC and NTP
        // is not guaranteed; the operator's laptop clock can also be
        // off by minutes. Using r.expiresAt directly produced bad
        // countdowns ("valid -30s" or infinite auto-refresh loops)
        // whenever the two clocks diverged. Subtracting the server-
        // derived lifetime (expiresAt - issuedAt) from Date.now()
        // makes the countdown depend on a duration the server is
        // authoritative on plus our local clock — drift-immune.
        // We sacrifice the network-latency portion of the TTL (server
        // started counting when it issued; we start counting now), but
        // that's the conservative direction and the server remains
        // the authority on the actual expiry.
        const lifetimeMs = Math.max(0, (r.expiresAt - r.issuedAt) * 1000);
        setTokenExpiresAtMs(Date.now() + lifetimeMs);
      })
      .catch((e: ApiError) => {
        setFetchFailed(true);
        setError(
          e.status === 401
            ? "Session expired — sign in again"
            : "Failed to fetch confirm token"
        );
      });
  }, [command]);

  const fetchTokenRef = useRef(fetchToken);
  fetchTokenRef.current = fetchToken;

  // Open: reset transient state and fetch the first token.
  useEffect(() => {
    if (!command) return;
    setConfirmed(false);
    fetchToken();
  }, [command, fetchToken]);

  // 1Hz ticker that drives the countdown render. Also handles
  // auto-refresh: when the live token's expiry rolls past `now`, fetch
  // a new one so the operator doesn't get a confusing 400 from the
  // server if they sat on the modal too long. Bounded by `fetchFailed`
  // so a backend error (e.g. 401) doesn't pin us in a refetch loop.
  useEffect(() => {
    if (!command) return;
    const id = setInterval(() => {
      const n = Date.now();
      setNowMs(n);
      if (!fetchFailed && tokenExpiresAtMs > 0 && n >= tokenExpiresAtMs) {
        fetchTokenRef.current();
      }
    }, 1000);
    return () => clearInterval(id);
  }, [command, tokenExpiresAtMs, fetchFailed]);

  if (!command) return null;
  const spec = specFor(command);

  // Seconds remaining on the current token. Negative-clamped; 0 while
  // an auto-refresh fetch is in flight (token cleared, expires reset).
  const secondsLeft = tokenExpiresAtMs > 0
    ? Math.max(0, Math.ceil((tokenExpiresAtMs - nowMs) / 1000))
    : 0;
  // Token is live and not currently being refreshed.
  const tokenLive = !!token && secondsLeft > 0;

  const submit = async () => {
    if (!tokenLive || !confirmed || submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      await runCommand(command, token);
      onSuccess();
    } catch (e: any) {
      const detail = e?.body?.detail;
      const msg =
        detail?.message ??
        (typeof detail === "string" ? detail : null) ??
        e?.message ??
        "Control command failed";
      setError(msg);
      // If the server rejected the token (race between client and
      // server clocks, or token consumed elsewhere), refresh it so
      // the operator can retry without closing/reopening the modal.
      const code = typeof detail === "object" && detail !== null ? detail.code : null;
      if (code === "token_invalid" || code === "token_expired") {
        fetchToken();
      }
    } finally {
      setSubmitting(false);
    }
  };

  // Countdown label shown beside the token. Distinguish in-flight
  // fetch (token empty) from expiry (token set, secondsLeft===0) so
  // operators see a clear "refreshing…" rather than the misleading
  // "0s" frozen for a moment.
  const countdownLabel =
    fetchFailed     ? "expired"
  : !token          ? "refreshing…"
  : secondsLeft <= 0 ? "refreshing…"
  :                   `valid ${secondsLeft}s`;

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
            disabled={!confirmed || !tokenLive || submitting}
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
        <span className="text-fa">Confirm token ({countdownLabel})</span>
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
