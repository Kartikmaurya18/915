import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { Check, Mail, Smartphone } from "lucide-react";
import { ApiError } from "../api/http";
import { useAuth, type Challenge, type Channel } from "../auth/AuthContext";
import { Button } from "../ui/Button";
import { FormMessage } from "../ui/feedback";

const LABEL: Record<Channel, { name: string; noun: string; Icon: typeof Mail }> = {
  email: { name: "Email", noun: "email address", Icon: Mail },
  phone: { name: "Mobile", noun: "mobile number", Icon: Smartphone },
};

/** Seconds left until `until`, ticking once a second. */
function useCountdown(): [number, (seconds: number) => void] {
  const [left, setLeft] = useState(0);
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);
  const start = useCallback((seconds: number) => {
    if (timer.current) clearInterval(timer.current);
    setLeft(Math.max(0, Math.ceil(seconds)));
    if (seconds <= 0) return;
    timer.current = setInterval(() => {
      setLeft((n) => {
        if (n <= 1 && timer.current) {
          clearInterval(timer.current);
          timer.current = null;
        }
        return Math.max(0, n - 1);
      });
    }, 1000);
  }, []);
  useEffect(() => () => void (timer.current && clearInterval(timer.current)), []);
  return [left, start];
}

interface Props {
  challenge: Challenge;
  /** The challenge changed (a channel was verified). */
  onChange: (next: Challenge) => void;
  /** Start over: the verification session ended or the person backed out. */
  onRestart: (message?: string) => void;
}

export function VerifyPanel({ challenge, onChange, onRestart }: Props) {
  // The channels this screen started with, so a verified one stays on screen as a tick.
  const [all] = useState<Channel[]>(challenge.pending);
  return (
    <div className="grid gap-4">
      <div>
        <h1 className="text-xl font-semibold tracking-tight">Verify your account</h1>
        <p className="mt-1 text-sm text-muted">
          We sent a {challenge.codeLength}-digit code to {all.length > 1 ? "each of these" : "this"}. Enter {all.length > 1 ? "both" : "it"} to finish.
        </p>
      </div>
      {all.map((channel) => (
        <ChannelRow key={channel} channel={channel} challenge={challenge} done={!challenge.pending.includes(channel)} onChange={onChange} onRestart={onRestart} />
      ))}
      <button type="button" onClick={() => onRestart()} className="justify-self-start text-sm font-medium text-brand-fg underline-offset-2 hover:underline">
        Use a different email or number
      </button>
    </div>
  );
}

function ChannelRow({ channel, challenge, done, onChange, onRestart }: { channel: Channel; challenge: Challenge; done: boolean; onChange: Props["onChange"]; onRestart: Props["onRestart"] }) {
  const { verify, resend } = useAuth();
  const { name, noun, Icon } = LABEL[channel];
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [wait, startWait] = useCountdown();
  const input = useRef<HTMLInputElement>(null);
  const first = useRef(true);

  useEffect(() => {
    startWait(challenge.resendAfterSeconds);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  useEffect(() => {
    if (first.current && !done) input.current?.focus();
    first.current = false;
  }, [done]);

  function fail(err: unknown) {
    if (err instanceof ApiError) {
      if (err.code === "verification_expired" || err.code === "invalid_verification_token") return onRestart("That verification session ended. Sign in to get new codes.");
      if (err.status === 429 && err.retryAfter) startWait(err.retryAfter);
      setError(err.message);
    } else setError("Something went wrong. Please try again.");
  }

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (busy || code.length < challenge.codeLength) return;
    setBusy(true);
    setError(null);
    setNote(null);
    try {
      const result = await verify(challenge, channel, code);
      if (result.kind === "verify") {
        setCode("");
        onChange(result.challenge);
      } // signed in: the page redirects on its own
    } catch (err) {
      fail(err);
      setCode("");
      input.current?.focus();
      setBusy(false);
      return;
    }
    setBusy(false);
  }

  async function again() {
    if (wait > 0 || busy) return;
    setError(null);
    setNote(null);
    try {
      startWait(await resend(challenge, channel));
      setNote(`A new code is on its way to your ${noun}.`);
      setCode("");
    } catch (err) {
      fail(err);
    }
  }

  const where = challenge.destinations[channel];
  if (done) {
    return (
      <div className="flex items-center gap-2 rounded-control border border-line bg-pos-soft px-3 py-2.5 text-sm text-pos" role="status">
        <Check aria-hidden="true" className="size-4 shrink-0" />
        <span className="whitespace-nowrap font-medium">{name} verified</span>
        {where && <span className="num ml-auto min-w-0 break-all text-xs opacity-80">{where}</span>}
      </div>
    );
  }
  const id = `code-${channel}`;
  return (
    <form onSubmit={submit} noValidate className="grid gap-2 rounded-control border border-line p-3">
      <label htmlFor={id} className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-sm font-medium">
        <Icon aria-hidden="true" className="size-4 text-brand-fg" />
        {name} code
        {where && <span className="num ml-auto min-w-0 break-all text-xs font-normal text-muted">sent to {where}</span>}
      </label>
      <div className="flex gap-2">
        <input
          ref={input}
          id={id}
          value={code}
          onChange={(e) => setCode(e.target.value.replace(/\D/g, "").slice(0, challenge.codeLength))}
          inputMode="numeric"
          autoComplete="one-time-code"
          pattern="[0-9]*"
          maxLength={challenge.codeLength}
          aria-invalid={error ? true : undefined}
          aria-describedby={error ? `${id}-error` : undefined}
          placeholder={"•".repeat(challenge.codeLength)}
          className="num min-h-10 w-full min-w-0 rounded-control border border-control bg-surface px-3 text-center text-lg tracking-[0.35em] text-fg placeholder:text-subtle transition-colors hover:border-fg/40 aria-[invalid=true]:border-neg"
        />
        <Button type="submit" variant="primary" loading={busy} disabled={code.length < challenge.codeLength}>
          Verify
        </Button>
      </div>
      {error && (
        <p id={`${id}-error`} role="alert" className="text-xs font-medium text-neg">
          {error}
        </p>
      )}
      {note && !error && <FormMessage ok>{note}</FormMessage>}
      <button
        type="button"
        onClick={again}
        disabled={wait > 0 || busy}
        className="justify-self-start text-sm font-medium text-brand-fg underline-offset-2 hover:underline disabled:cursor-not-allowed disabled:text-muted disabled:no-underline"
      >
        {wait > 0 ? `Send a new code in ${wait}s` : "Send a new code"}
      </button>
    </form>
  );
}

