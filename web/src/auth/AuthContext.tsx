import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { ApiError, request, setAccessToken, setRefreshHandler, type AuthUserInfo, type TokenResponse } from "../api/http";

/**
 * Session model
 *  - the short-lived access token lives only in memory (never localStorage);
 *  - the refresh token is an HttpOnly cookie, rotated on every refresh by the server;
 *  - `md_session` in localStorage is a non-secret hint ("this browser had a session") so a first-time
 *    visitor does not make a pointless /refresh call (which the server would count as a failed login);
 *  - the server may run with AUTH_MODE=disabled (local development): detected by probing /api/auth/me.
 */

export type AuthMode = "required" | "disabled";
export type AuthStatus = "loading" | "anonymous" | "authenticated";

export type Channel = "email" | "phone";

/** A sign-up or sign-in that must still prove its email and/or mobile number with one-time codes. */
export interface Challenge {
  token: string;
  pending: Channel[];
  /** Masked, e.g. "a***@example.com" and "+91*******210". */
  destinations: Partial<Record<Channel, string>>;
  resendAfterSeconds: number;
  codeLength: number;
}

export type AuthResult = { kind: "signed-in" } | { kind: "verify"; challenge: Challenge };

export interface RegisterInput {
  email: string;
  password: string;
  /** E.164, e.g. "+919876543210". Omit when the server does not verify phones. */
  phone?: string;
}

export interface AuthContextValue {
  status: AuthStatus;
  mode: AuthMode | null;
  user: AuthUserInfo | null;
  /** Increments whenever a new access token was issued (the realtime client renews on it). */
  tokenVersion: number;
  login(email: string, password: string): Promise<AuthResult>;
  register(input: RegisterInput): Promise<AuthResult>;
  /** Check one code. Resolves to signed-in when it was the last one needed, else the challenge that remains. */
  verify(challenge: Challenge, channel: Channel, code: string): Promise<AuthResult>;
  /** Ask for a fresh code. Resolves with the seconds until another may be requested. */
  resend(challenge: Challenge, channel: Channel): Promise<number>;
  forgotPassword(email: string, channel: Channel): Promise<void>;
  resetPassword(input: { email: string; channel: Channel; code: string; newPassword: string }): Promise<void>;
  logout(): Promise<void>;
}

const SESSION_HINT = "md_session";
const REFRESH_LEAD_MS = 60_000;
const MIN_REFRESH_DELAY_MS = 5_000;
const LOCAL_USER: AuthUserInfo = { id: "local-dev", role: "admin", email: "local-dev" };

const AuthContext = createContext<AuthContextValue | null>(null);

function hint(set?: boolean): boolean {
  try {
    if (set === true) localStorage.setItem(SESSION_HINT, "1");
    else if (set === false) localStorage.removeItem(SESSION_HINT);
    return localStorage.getItem(SESSION_HINT) === "1";
  } catch {
    return false; // storage blocked (private mode): fall back to always probing
  }
}

/** Serialise refreshes across tabs: concurrent use of one rotating token would trip replay detection. */
async function withRefreshLock<T>(fn: () => Promise<T>): Promise<T> {
  const locks = (navigator as Navigator & { locks?: LockManager }).locks;
  return locks ? locks.request("md-refresh", fn) : fn();
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<AuthStatus>("loading");
  const [mode, setMode] = useState<AuthMode | null>(null);
  const [user, setUser] = useState<AuthUserInfo | null>(null);
  const [tokenVersion, setTokenVersion] = useState(0);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const refreshRef = useRef<() => Promise<boolean>>(async () => false);

  const clearSession = useCallback(() => {
    setAccessToken(null);
    if (timer.current) clearTimeout(timer.current);
    timer.current = null;
    hint(false);
    setUser(null);
    setStatus("anonymous");
  }, []);

  const adopt = useCallback((res: TokenResponse) => {
    setAccessToken(res.access_token);
    hint(true);
    setUser(res.user);
    setStatus("authenticated");
    setTokenVersion((v) => v + 1);
    if (timer.current) clearTimeout(timer.current);
    const ttl = new Date(res.expires_at).getTime() - Date.now();
    timer.current = setTimeout(() => void refreshRef.current(), Math.max(MIN_REFRESH_DELAY_MS, ttl - REFRESH_LEAD_MS));
  }, []);

  const refresh = useCallback(async (): Promise<boolean> => {
    try {
      const res = await withRefreshLock(() =>
        request<TokenResponse>("/api/auth/refresh", { method: "POST", body: {}, credentials: true, noRefresh: true }),
      );
      adopt(res);
      return true;
    } catch (err) {
      // 429 / network trouble is not a logout: keep the session and let the next attempt retry.
      if (err instanceof ApiError && (err.status === 401 || err.status === 403)) clearSession();
      else if (!(err instanceof ApiError) || err.status === 429 || err.status >= 500) {
        if (timer.current) clearTimeout(timer.current);
        timer.current = setTimeout(() => void refreshRef.current(), 15_000);
      }
      return false;
    }
  }, [adopt, clearSession]);
  refreshRef.current = refresh;

  useEffect(() => {
    setRefreshHandler(refresh);
    return () => setRefreshHandler(null);
  }, [refresh]);

  // Bootstrap: which mode is the server in, and is there a session to resume?
  useEffect(() => {
    let cancelled = false;
    const disabled = () => {
      if (cancelled) return;
      setMode("disabled");
      setUser(LOCAL_USER);
      setStatus("authenticated");
    };
    void (async () => {
      if (hint()) {
        // A session existed in this browser: resume it directly. Auth disabled shows up as a 404 here.
        setMode("required");
        try {
          const res = await withRefreshLock(() =>
            request<TokenResponse>("/api/auth/refresh", { method: "POST", body: {}, credentials: true, noRefresh: true }),
          );
          if (!cancelled) adopt(res);
          return;
        } catch (err) {
          if (err instanceof ApiError && err.status === 404) return disabled();
          if (cancelled) return;
          if (err instanceof ApiError && (err.status === 401 || err.status === 403)) return clearSession();
          // Network trouble or a rate limit: keep the hint, retry shortly, show the sign-in screen meanwhile.
          setStatus("anonymous");
          timer.current = setTimeout(() => void refreshRef.current(), 5_000);
          return;
        }
      }
      // No hint: ask whoever answers /me without a token. 404 = auth disabled, 401 = sign-in required.
      try {
        await request("/api/auth/me", { noRefresh: true });
        disabled();
      } catch (err) {
        if (cancelled) return;
        if (err instanceof ApiError && err.status === 404) return disabled();
        setMode("required");
        setStatus("anonymous");
      }
    })();
    return () => {
      cancelled = true;
      if (timer.current) clearTimeout(timer.current);
    };
  }, [adopt, clearSession]);

  /** The server's "verify first" answers (a 202 from sign-up, a 403 from sign-in) become one shape. */
  const toChallenge = (d: Record<string, unknown>): Challenge => ({
    token: String(d.verification_token),
    pending: (d.pending as Channel[]) ?? [],
    destinations: (d.destinations as Challenge["destinations"]) ?? {},
    resendAfterSeconds: Number(d.resend_after_seconds ?? 60),
    codeLength: Number(d.code_length ?? 6),
  });

  const login = useCallback(
    async (email: string, password: string): Promise<AuthResult> => {
      try {
        adopt(await request<TokenResponse>("/api/auth/login", { method: "POST", body: { email, password }, credentials: true, noRefresh: true }));
        return { kind: "signed-in" };
      } catch (err) {
        if (err instanceof ApiError && err.code === "verification_required") return { kind: "verify", challenge: toChallenge(err.data) };
        throw err;
      }
    },
    [adopt],
  );
  const register = useCallback(
    async ({ email, password, phone }: RegisterInput): Promise<AuthResult> => {
      const res = await request<TokenResponse | (Record<string, unknown> & { verification_required: true })>("/api/auth/register", {
        method: "POST",
        body: phone ? { email, password, phone } : { email, password },
        credentials: true,
        noRefresh: true,
      });
      if ("verification_required" in res && res.verification_required) return { kind: "verify", challenge: toChallenge(res) };
      adopt(res as TokenResponse);
      return { kind: "signed-in" };
    },
    [adopt],
  );
  const verify = useCallback(
    async (challenge: Challenge, channel: Channel, code: string): Promise<AuthResult> => {
      const res = await request<(TokenResponse & { verification_required: false }) | (Record<string, unknown> & { verification_required: true })>("/api/auth/verify", {
        method: "POST",
        body: { verification_token: challenge.token, channel, code },
        credentials: true,
        noRefresh: true,
      });
      if (res.verification_required) {
        return { kind: "verify", challenge: { ...challenge, pending: (res.pending as Channel[]) ?? [] } };
      }
      adopt(res as TokenResponse);
      return { kind: "signed-in" };
    },
    [adopt],
  );
  const resend = useCallback(async (challenge: Challenge, channel: Channel): Promise<number> => {
    const res = await request<{ resend_after_seconds?: number }>("/api/auth/resend", {
      method: "POST",
      body: { verification_token: challenge.token, channel },
      credentials: true,
      noRefresh: true,
    });
    return Number(res.resend_after_seconds ?? 60);
  }, []);
  const forgotPassword = useCallback(async (email: string, channel: Channel) => {
    await request("/api/auth/password/forgot", { method: "POST", body: { email, channel }, credentials: true, noRefresh: true });
  }, []);
  const resetPassword = useCallback(async ({ email, channel, code, newPassword }: { email: string; channel: Channel; code: string; newPassword: string }) => {
    await request("/api/auth/password/reset", { method: "POST", body: { email, channel, code, new_password: newPassword }, credentials: true, noRefresh: true });
  }, []);
  const logout = useCallback(async () => {
    try {
      await request("/api/auth/logout", { method: "POST", body: {}, credentials: true, noRefresh: true });
    } catch {
      /* the server-side revoke is best effort; the local session ends regardless */
    }
    clearSession();
  }, [clearSession]);

  const value = useMemo<AuthContextValue>(
    () => ({ status, mode, user, tokenVersion, login, register, verify, resend, forgotPassword, resetPassword, logout }),
    [status, mode, user, tokenVersion, login, register, verify, resend, forgotPassword, resetPassword, logout],
  );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used inside <AuthProvider>");
  return ctx;
}
