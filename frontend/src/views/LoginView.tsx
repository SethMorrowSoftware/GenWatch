// Login screen — single-password, set in config.yaml.

import { useState } from "react";
import { api, ApiError } from "../api/client";
import { BrandMark, Icon } from "../components/primitives";

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
    <div className="login-shell">
      <form onSubmit={submit} className="login-card">
        <div className="login-brand">
          <BrandMark size={56} />
          <div>
            <div className="login-title">Castle Generator Monitor</div>
            <div className="login-sub">Operator Console · v0.1</div>
          </div>
        </div>
        <p className="login-prompt">
          Sign in to view live telemetry and issue control commands.
        </p>
        <label className="login-field">
          <span>Admin password</span>
          <input
            className="input login-input"
            type="password"
            autoFocus
            placeholder="••••••••"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </label>
        {error && (
          <div className="login-error" role="alert">
            <Icon name="x" size={14} />
            <span>{error}</span>
          </div>
        )}
        <button type="submit" className="btn btn-primary login-submit"
                disabled={submitting || !password}>
          <Icon name="lock" size={14} /> {submitting ? "Signing in…" : "Sign in"}
        </button>
      </form>
      <div className="login-foot">
        <span className="dot" />
        Hardware safeties at the H-100 panel remain primary.
      </div>
    </div>
  );
}
