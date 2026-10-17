import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { ShieldCheck, TrendingUp } from "lucide-react";
import { ApiError } from "../api/http";
import { useAuth } from "../auth/AuthContext";
import { Button } from "../ui/Button";
import { ErrorState } from "../ui/feedback";
import { Field, Input } from "../ui/fields";

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
    <main id="main" className="grid min-h-dvh place-items-center px-4 py-10">
      <div className="w-full max-w-sm">
        <div className="mb-6 flex items-center justify-center gap-2.5">
          <span aria-hidden="true" className="grid size-10 place-items-center rounded-card bg-brand text-on-brand shadow-card">
            <TrendingUp className="size-5" />
          </span>
          <span className="text-lg font-semibold tracking-tight">Market Data</span>
        </div>
        <form onSubmit={onSubmit} noValidate className="grid gap-4 rounded-card border border-line bg-surface p-6 shadow-card sm:p-7">
          <div>
            <h1 className="text-xl font-semibold tracking-tight">{isLogin ? "Sign in" : "Create account"}</h1>
            <p className="mt-1 flex items-start gap-1.5 text-sm text-muted">
              <ShieldCheck aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-brand-fg" />
              Simulated trading only. No real orders are ever placed.
            </p>
          </div>

          <Field label="Email">{(p) => <Input {...p} type="email" autoComplete="email" inputMode="email" required value={email} onChange={(e) => setEmail(e.target.value)} className="text-left" />}</Field>
          <Field label="Password" hint={isLogin ? undefined : `At least ${MIN_PASSWORD} characters.`}>
            {(p) => (
              <Input
                {...p}
                type="password"
                autoComplete={isLogin ? "current-password" : "new-password"}
                required
                minLength={isLogin ? undefined : MIN_PASSWORD}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                className="text-left"
              />
            )}
          </Field>

          {error && <ErrorState>{error}</ErrorState>}
          <Button type="submit" variant="primary" size="lg" loading={busy} disabled={!email || !password}>
            {busy ? "Please wait…" : isLogin ? "Sign in" : "Create account"}
          </Button>
          <p className="text-center text-sm text-muted">
            {isLogin ? (
              <>
                New here?{" "}
                <Link to="/register" className="font-medium text-brand-fg underline-offset-2 hover:underline">
                  Create an account
                </Link>
              </>
            ) : (
              <>
                Already registered?{" "}
                <Link to="/login" className="font-medium text-brand-fg underline-offset-2 hover:underline">
                  Sign in
                </Link>
              </>
            )}
          </p>
        </form>
      </div>
    </main>
  );
}
