import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { ApiError } from "../api/http";
import { useAuth } from "../auth/AuthContext";

const MIN_PASSWORD = 10;

export function AuthPage({ kind }: { kind: "login" | "register" }) {
  const { login, register } = useAuth();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const isLogin = kind === "login";

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    if (busy) return;
    setError(null);
    if (!isLogin && password.length < MIN_PASSWORD) {
      setError(`Password must be at least ${MIN_PASSWORD} characters.`);
      return;
    }
    setBusy(true);
    try {
      await (isLogin ? login(email, password) : register(email, password));
      // RedirectIfAuthed moves us into the app as soon as the session exists.
    } catch (err) {
      if (err instanceof ApiError && err.status === 429) {
        const wait = err.retryAfter ? ` Try again in about ${Math.ceil(err.retryAfter / 60)} min.` : "";
        setError("Too many attempts." + wait);
      } else {
        setError(err instanceof Error ? err.message : "Something went wrong. Please try again.");
      }
      setBusy(false);
    }
  }

  return (
    <div className="auth-wrap">
      <form className="panel auth-card" onSubmit={onSubmit} noValidate>
        <h1>{isLogin ? "Sign in" : "Create account"}</h1>
        <p className="muted">Simulated trading only. No real orders are ever placed.</p>

        <label>
          Email
          <input type="email" autoComplete="email" inputMode="email" required value={email} onChange={(e) => setEmail(e.target.value)} />
        </label>
        <label>
          Password
          <input
            type="password"
            autoComplete={isLogin ? "current-password" : "new-password"}
            required
            minLength={isLogin ? undefined : MIN_PASSWORD}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
          {!isLogin && <span className="hint">At least {MIN_PASSWORD} characters.</span>}
        </label>

        {error && (
          <p className="form-error" role="alert">
            {error}
          </p>
        )}
        <button className="btn btn-primary" type="submit" disabled={busy || !email || !password}>
          {busy ? "Please wait…" : isLogin ? "Sign in" : "Create account"}
        </button>
        <p className="muted switch">
          {isLogin ? (
            <>
              New here? <Link to="/register">Create an account</Link>
            </>
          ) : (
            <>
              Already registered? <Link to="/login">Sign in</Link>
            </>
          )}
        </p>
      </form>
    </div>
  );
}
