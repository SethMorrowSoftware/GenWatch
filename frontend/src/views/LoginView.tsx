// Login screen — single-password, set in config.yaml.

import { useState } from "react";
import { api, ApiError } from "../api/client";
import { Icon } from "../components/primitives";

export function LoginView({ onLoggedIn }: { onLoggedIn: () => void }) {
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!password || submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      await api.login(password);
      onLoggedIn();
    } catch (e: any) {
      if (e instanceof ApiError && e.status === 401) setError("Incorrect password.");
      else if (e instanceof ApiError && e.status === 503) setError("Auth not initialized — set admin_password_hash in config.yaml.");
      else setError(e?.message ?? "Login failed");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div style={{
      minHeight: "100vh", display: "flex", alignItems: "center", justifyContent: "center",
      padding: 24,
    }}>
      <form onSubmit={submit} style={{
        width: 360, padding: 28, background: "var(--panel)",
        border: "1px solid var(--border)", borderRadius: "var(--rad-l)",
        boxShadow: "var(--shadow)",
        display: "flex", flexDirection: "column", gap: 14,
      }}>
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <div className="brand-mark" />
          <div className="brand-name">GenWatch <span>v0.1</span></div>
        </div>
        <p style={{ color: "var(--text-3)", fontSize: 13, margin: 0 }}>
          Sign in to view live telemetry and issue control commands.
        </p>
        <input
          className="input"
          type="password"
          autoFocus
          placeholder="Admin password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          style={{ padding: "10px 12px", fontSize: 14, fontFamily: "var(--sans)" }}
        />
        {error && (
          <div style={{
            padding: 10, background: "color-mix(in oklch, var(--red) 10%, var(--panel-2))",
            color: "var(--red)", borderRadius: 7, fontSize: 12.5,
            border: "1px solid color-mix(in oklch, var(--red) 35%, var(--border))",
          }}>{error}</div>
        )}
        <button type="submit" className="btn btn-primary" disabled={submitting || !password}
                style={{ justifyContent: "center", padding: "10px 16px" }}>
          <Icon name="lock" size={14} /> {submitting ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </div>
  );
}
