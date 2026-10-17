import { useEffect, useState, type FormEvent, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { ShieldCheck, TrendingUp } from "lucide-react";
import { ApiError, request } from "../api/http";
import { useAuth, type Challenge } from "../auth/AuthContext";
import { PhoneField } from "../components/PhoneField";
import { VerifyPanel } from "../components/VerifyPanel";
import { DEFAULT_COUNTRY, buildE164 } from "../lib/phone";
import { Button } from "../ui/Button";
import { ErrorState } from "../ui/feedback";
import { Field, Input } from "../ui/fields";

const MIN_PASSWORD = 10;

interface SignUpOptions {
  channels: string[];
  registration_open: boolean;
}

/** What the sign-up form must ask for. If the server cannot say, offer the phone field: a wrong guess only costs an optional field. */
function useSignUpOptions(enabled: boolean): { askPhone: boolean; closed: boolean } {
  const [options, setOptions] = useState<SignUpOptions | null | "failed">(null);
  useEffect(() => {
    if (!enabled) return;
    const ctrl = new AbortController();
    request<SignUpOptions>("/api/auth/options", { signal: ctrl.signal, noRefresh: true })
      .then((o) => !ctrl.signal.aborted && setOptions(o))
      .catch(() => !ctrl.signal.aborted && setOptions("failed"));
    return () => ctrl.abort();
  }, [enabled]);
  if (options === null) return { askPhone: false, closed: false };
  if (options === "failed") return { askPhone: true, closed: false };
  return { askPhone: options.channels.includes("phone"), closed: !options.registration_open };
}

export function AuthShell({ children }: { children: ReactNode }) {
  return (
    <main id="main" className="grid min-h-dvh place-items-center px-4 py-10">
      <div className="w-full max-w-sm">
        <div className="mb-6 flex items-center justify-center gap-2.5">
          <span aria-hidden="true" className="grid size-10 place-items-center rounded-card bg-brand text-on-brand shadow-card">
            <TrendingUp className="size-5" />
          </span>
          <span className="text-lg font-semibold tracking-tight">Market Data</span>
        </div>
        <div className="grid gap-4 rounded-card border border-line bg-surface p-6 shadow-card sm:p-7">{children}</div>
      </div>
    </main>
  );
}

export function AuthPage({ kind }: { kind: "login" | "register" }) {
  const { login, register } = useAuth();
  const isLogin = kind === "login";
  const { askPhone, closed } = useSignUpOptions(!isLogin);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [country, setCountry] = useState(DEFAULT_COUNTRY);
  const [national, setNational] = useState("");
  const [phoneError, setPhoneError] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [challenge, setChallenge] = useState<Challenge | null>(null);
  const [busy, setBusy] = useState(false);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    if (busy) return;
    setError(null);
    setNotice(null);
    setPhoneError(null);
    if (!isLogin && password.length < MIN_PASSWORD) {
      setError(`Password must be at least ${MIN_PASSWORD} characters.`);
      return;
    }
    const phone = !isLogin && askPhone ? buildE164(country, national) : null;
    if (!isLogin && askPhone && !phone) {
      setPhoneError("Enter a valid mobile number.");
      return;
    }
    setBusy(true);
    try {
      const result = isLogin ? await login(email, password) : await register({ email, password, phone: phone ?? undefined });
      if (result.kind === "verify") setChallenge(result.challenge);
      // Otherwise a session exists and RedirectIfAuthed moves us into the app.
    } catch (err) {
      if (err instanceof ApiError && err.status === 429) {
        const wait = err.retryAfter ? ` Try again in about ${Math.max(1, Math.ceil(err.retryAfter / 60))} min.` : "";
        setError("Too many attempts." + wait);
      } else if (err instanceof ApiError && err.code === "invalid_phone") {
        setPhoneError(err.message);
      } else {
        setError(err instanceof Error ? err.message : "Something went wrong. Please try again.");
      }
    }
    setBusy(false);
  }

  if (challenge) {
    return (
      <AuthShell>
        <VerifyPanel
          challenge={challenge}
          onChange={setChallenge}
          onRestart={(message) => {
            setChallenge(null);
            setPassword("");
            if (message) setNotice(message);
          }}
        />
      </AuthShell>
    );
  }

  return (
    <AuthShell>
      <form onSubmit={onSubmit} noValidate className="grid gap-4">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">{isLogin ? "Sign in" : "Create account"}</h1>
          <p className="mt-1 flex items-start gap-1.5 text-sm text-muted">
            <ShieldCheck aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-brand-fg" />
            Simulated trading only. No real orders are ever placed.
          </p>
        </div>

        {notice && (
          <p role="status" className="rounded-control border border-line bg-surface-2 px-3 py-2 text-sm">
            {notice}
          </p>
        )}
        {!isLogin && closed && <ErrorState>Registration is currently closed.</ErrorState>}

        <Field label="Email">{(p) => <Input {...p} type="email" autoComplete="email" inputMode="email" required value={email} onChange={(e) => setEmail(e.target.value)} className="text-left" />}</Field>
        {!isLogin && askPhone && <PhoneField country={country} national={national} onCountry={setCountry} onNational={setNational} error={phoneError} />}
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
        {isLogin && (
          <Link to="/forgot" className="-mt-2 justify-self-start text-sm font-medium text-brand-fg underline-offset-2 hover:underline">
            Forgot your password?
          </Link>
        )}

        {error && <ErrorState>{error}</ErrorState>}
        <Button type="submit" variant="primary" size="lg" loading={busy} disabled={!email || !password || (!isLogin && askPhone && !national) || (!isLogin && closed)}>
          {busy ? "Please wait…" : isLogin ? "Sign in" : askPhone ? "Create account and send codes" : "Create account"}
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
    </AuthShell>
  );
}
