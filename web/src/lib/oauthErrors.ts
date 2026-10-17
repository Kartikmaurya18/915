/**
 * What to tell someone when "Continue with Google" did not work. The server redirects to /login?error=<code>; only codes
 * listed here are ever shown, so nothing a stranger puts in a URL is echoed onto the page.
 */
const MESSAGES: Record<string, string> = {
  oauth_denied: "Sign-in was cancelled. You can try again, or sign in with your email.",
  oauth_expired: "That sign-in attempt expired or was not started in this browser. Please try again.",
  oauth_email_unverified: "That account's email address has not been verified by the provider, so we cannot use it. Verify it there, or sign up with your email.",
  oauth_email_missing: "The provider did not share an email address, which we need to create your account.",
  oauth_account_disabled: "This account has been disabled.",
  oauth_registration_closed: "Registration is currently closed.",
  oauth_failed: "We could not sign you in with that provider. Please try again, or use your email.",
  rate_limited: "Too many attempts. Try again in a few minutes.",
};

export function oauthErrorMessage(code: string | null | undefined): string | null {
  return code && Object.prototype.hasOwnProperty.call(MESSAGES, code) ? MESSAGES[code]! : null;
}
