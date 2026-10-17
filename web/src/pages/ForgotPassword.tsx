import { useEffect, useRef, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { ApiError } from "../api/http";
import { useAuth, type Channel } from "../auth/AuthContext";
import { Button } from "../ui/Button";
import { ErrorState, FormMessage } from "../ui/feedback";
import { Field, Input } from "../ui/fields";
import { AuthShell } from "./AuthPage";

const MIN_PASSWORD = 10;
const CODE_LENGTH = 6;

/** Forgot password: ask for a code on a verified email or mobile, then set a new password with it. */
export default function ForgotPassword() {
  const { forgotPassword, resetPassword } = useAuth();
  const [step, setStep] = useState<"ask" | "reset" | "done">("ask");
  const [email, setEmail] = useState("");
  const [channel, setChannel] = useState<Channel>("email");
  const [code, setCode] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [wait, setWait] = useState(0);
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => () => void (timer.current && clearInterval(timer.current)), []);
  function startWait(seconds: number) {
    if (timer.current) clearInterval(timer.current);
    setWait(seconds);
    timer.current = setInterval(() => setWait((n) => (n <= 1 ? (timer.current && clearInterval(timer.current), 0) : n - 1)), 1000);
  }

  function describe(err: unknown): string {
    if (err instanceof ApiError && err.status === 429) return err.retryAfter && err.retryAfter > 60 ? `Too many attempts. Try again in about ${Math.ceil(err.retryAfter / 60)} min.` : err.message;
    return err instanceof Error ? err.message : "Something went wrong. Please try again.";
  }

  async function ask(e?: FormEvent) {
    e?.preventDefault();
    if (busy || !email) return;
    setBusy(true);
    setError(null);
    try {
      await forgotPassword(email, channel);
      setStep("reset");
      setNote(`If that account exists, a code is on its way to its verified ${channel === "email" ? "email address" : "mobile number"}.`);
      startWait(60);
    } catch (err) {
      setError(describe(err));
    }
    setBusy(false);
  }

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (busy) return;
    setError(null);
    if (password.length < MIN_PASSWORD) return setError(`Password must be at least ${MIN_PASSWORD} characters.`);
    setBusy(true);
    try {
      await resetPassword({ email, channel, code, newPassword: password });
      setStep("done");
    } catch (err) {
      setError(describe(err));
      setCode("");
    }
    setBusy(false);
  }

  if (step === "done") {
    return (
      <AuthShell>
        <h1 className="text-xl font-semibold tracking-tight">Password changed</h1>
        <FormMessage ok>You have been signed out everywhere. Sign in with your new password.</FormMessage>
        <Link to="/login" className="inline-flex min-h-12 items-center justify-center rounded-control bg-brand px-5 text-base font-medium text-on-brand hover:brightness-110">
          Go to sign in
        </Link>
      </AuthShell>
    );
  }

  return (
    <AuthShell>
      <div>
        <h1 className="text-xl font-semibold tracking-tight">Reset your password</h1>
        <p className="mt-1 text-sm text-muted">We will send a one-time code to a contact you have already verified.</p>
      </div>

      {step === "ask" ? (
        <form onSubmit={ask} noValidate className="grid gap-4">
          <Field label="Email">{(p) => <Input {...p} type="email" autoComplete="email" inputMode="email" required value={email} onChange={(e) => setEmail(e.target.value)} className="text-left" />}</Field>
          <fieldset className="grid gap-1.5">
            <legend className="text-sm font-medium">Send the code to</legend>
            <div className="flex gap-4 text-sm">
              {(["email", "phone"] as const).map((c) => (
                <label key={c} className="flex min-h-8 items-center gap-2">
                  <input type="radio" name="channel" value={c} checked={channel === c} onChange={() => setChannel(c)} className="size-4 accent-[var(--brand)]" />
                  {c === "email" ? "My email" : "My mobile"}
                </label>
              ))}
            </div>
          </fieldset>
          {error && <ErrorState>{error}</ErrorState>}
          <Button type="submit" variant="primary" size="lg" loading={busy} disabled={!email}>
            Send code
          </Button>
        </form>
      ) : (
        <form onSubmit={submit} noValidate className="grid gap-4">
          {note && (
            <p role="status" className="rounded-control border border-line bg-surface-2 px-3 py-2 text-sm">
              {note}
            </p>
          )}
          <Field label="Code">
            {(p) => (
              <Input
                {...p}
                value={code}
                onChange={(e) => setCode(e.target.value.replace(/\D/g, "").slice(0, CODE_LENGTH))}
                inputMode="numeric"
                autoComplete="one-time-code"
                maxLength={CODE_LENGTH}
                className="text-center text-lg tracking-[0.35em]"
                placeholder={"•".repeat(CODE_LENGTH)}
              />
            )}
          </Field>
          <Field label="New password" hint={`At least ${MIN_PASSWORD} characters.`}>
            {(p) => <Input {...p} type="password" autoComplete="new-password" required value={password} onChange={(e) => setPassword(e.target.value)} className="text-left" />}
          </Field>
          {error && <ErrorState>{error}</ErrorState>}
          <Button type="submit" variant="primary" size="lg" loading={busy} disabled={code.length < CODE_LENGTH || !password}>
            Change password
          </Button>
          <button
            type="button"
            onClick={() => void ask()}
            disabled={wait > 0 || busy}
            className="justify-self-start text-sm font-medium text-brand-fg underline-offset-2 hover:underline disabled:cursor-not-allowed disabled:text-muted disabled:no-underline"
          >
            {wait > 0 ? `Send a new code in ${wait}s` : "Send a new code"}
          </button>
        </form>
      )}
      <p className="text-center text-sm text-muted">
        <Link to="/login" className="font-medium text-brand-fg underline-offset-2 hover:underline">
          Back to sign in
        </Link>
      </p>
    </AuthShell>
  );
}
