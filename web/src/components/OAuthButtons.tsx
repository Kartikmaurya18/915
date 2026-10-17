import { KeyRound } from "lucide-react";

export interface OAuthProvider {
  id: string;
  label: string;
}

/** Google's "G" in its brand colours (the mark Google's sign-in guidelines ask for). */
function GoogleMark() {
  return (
    <svg aria-hidden="true" viewBox="0 0 48 48" className="size-5 shrink-0">
      <path fill="#EA4335" d="M24 9.5c3.5 0 6.6 1.2 9.1 3.6l6.8-6.8C35.8 2.4 30.3 0 24 0 14.6 0 6.5 5.4 2.6 13.2l7.9 6.2C12.4 13.6 17.7 9.5 24 9.5z" />
      <path fill="#4285F4" d="M46.1 24.5c0-1.6-.1-3.1-.4-4.5H24v9h12.4c-.5 2.9-2.2 5.3-4.6 6.9l7.4 5.7c4.3-4 6.9-9.9 6.9-17.1z" />
      <path fill="#FBBC05" d="M10.5 28.6c-.5-1.4-.8-2.9-.8-4.6s.3-3.2.8-4.6l-7.9-6.2C.9 16.5 0 20.1 0 24s.9 7.5 2.6 10.8l7.9-6.2z" />
      <path fill="#34A853" d="M24 48c6.5 0 11.9-2.1 15.9-5.8l-7.4-5.7c-2.1 1.4-4.9 2.3-8.5 2.3-6.3 0-11.6-4.1-13.5-9.9l-7.9 6.2C6.5 42.6 14.6 48 24 48z" />
    </svg>
  );
}

/**
 * "Continue with ..." buttons. They are plain links to the server, which starts the provider's sign-in and sends the
 * browser back: a normal navigation, so they work without script and the browser, not this page, handles the redirect.
 */
export function OAuthButtons({ providers }: { providers: readonly OAuthProvider[] }) {
  if (providers.length === 0) return null;
  return (
    <div className="grid gap-3" role="group" aria-label="Sign in with another account">
      {providers.map((p) => (
        <a
          key={p.id}
          href={`/api/auth/oauth/${encodeURIComponent(p.id)}/start`}
          className="inline-flex min-h-12 items-center justify-center gap-3 rounded-control border border-control bg-surface px-5 text-base font-medium text-fg transition-colors hover:bg-surface-2"
        >
          {p.id === "google" ? <GoogleMark /> : <KeyRound aria-hidden="true" className="size-5 shrink-0" />}
          Continue with {p.label}
        </a>
      ))}
      <div className="flex items-center gap-3 text-xs text-muted" aria-hidden="true">
        <span className="h-px flex-1 bg-line" />
        or use your email
        <span className="h-px flex-1 bg-line" />
      </div>
    </div>
  );
}
