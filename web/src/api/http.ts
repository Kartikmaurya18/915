/**
 * Fetch wrapper: bearer token from memory, one transparent refresh on an expired token,
 * structured errors. The refresh token itself is an HttpOnly cookie the page cannot read.
 */

export class ApiError extends Error {
  status: number;
  code: string;
  retryAfter: number | null;
  /** Extra fields the server sent with the error (pending channels, attempts left, ...). Never secrets. */
  data: Record<string, unknown>;
  constructor(status: number, code: string, message: string, retryAfter: number | null = null, data: Record<string, unknown> = {}) {
    super(message);
    this.status = status;
    this.code = code;
    this.retryAfter = retryAfter;
    this.data = data;
  }
}

export interface AuthUserInfo {
  id: string;
  email?: string;
  role: "user" | "admin";
}

export interface TokenResponse {
  access_token: string;
  token_type: string;
  expires_at: string;
  user: AuthUserInfo;
}

type RefreshFn = () => Promise<boolean>;

let accessToken: string | null = null;
let refreshFn: RefreshFn | null = null;
let inflightRefresh: Promise<boolean> | null = null;

export function setAccessToken(token: string | null): void {
  accessToken = token;
}
export function getAccessToken(): string | null {
  return accessToken;
}
/** The auth provider registers how to get a new access token (single-flight below). */
export function setRefreshHandler(fn: RefreshFn | null): void {
  refreshFn = fn;
}

/** Share one refresh among all callers that hit an expired token at the same moment. */
export function refreshOnce(): Promise<boolean> {
  if (!refreshFn) return Promise.resolve(false);
  if (!inflightRefresh) {
    inflightRefresh = refreshFn().finally(() => {
      inflightRefresh = null;
    });
  }
  return inflightRefresh;
}

async function toError(res: Response): Promise<ApiError> {
  let code = "http_" + res.status;
  let message = res.statusText || "Request failed";
  let data: Record<string, unknown> = {};
  try {
    const body = (await res.json()) as { detail?: unknown };
    const detail = body?.detail;
    if (typeof detail === "string") message = detail;
    else if (Array.isArray(detail)) {
      // FastAPI validation errors: [{loc, msg, type}, ...]
      if (detail[0]?.msg) message = String(detail[0].msg);
      code = "validation";
    } else if (detail && typeof detail === "object") {
      const d = detail as { code?: string; message?: string };
      code = d.code ?? code;
      message = d.message ?? message;
      data = detail as Record<string, unknown>;
    }
  } catch {
    /* non-JSON error body */
  }
  const retry = res.headers.get("Retry-After");
  const fromBody = typeof data.retry_after === "number" ? data.retry_after : null;
  return new ApiError(res.status, code, message, retry ? Number(retry) : fromBody, data);
}

export interface RequestOptions {
  method?: string;
  body?: unknown;
  headers?: Record<string, string>;
  signal?: AbortSignal;
  /** Send the CSRF header and cookies (auth endpoints). */
  credentials?: boolean;
  /** Do not try to refresh on 401 (used by the refresh call itself). */
  noRefresh?: boolean;
}

export async function request<T>(path: string, opts: RequestOptions = {}, retried = false): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json", ...opts.headers };
  if (opts.body !== undefined) headers["Content-Type"] = "application/json";
  if (accessToken) headers["Authorization"] = `Bearer ${accessToken}`;
  if (opts.credentials) headers["X-Requested-With"] = "fetch";

  const res = await fetch(path, {
    method: opts.method ?? "GET",
    headers,
    body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
    signal: opts.signal,
    credentials: "same-origin",
  });

  if (res.status === 401 && !retried && !opts.noRefresh && (await refreshOnce())) {
    return request<T>(path, opts, true);
  }
  if (!res.ok) throw await toError(res);
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export const api = {
  get: <T>(path: string, signal?: AbortSignal) => request<T>(path, { signal }),
  post: <T>(path: string, body?: unknown, headers?: Record<string, string>) =>
    request<T>(path, { method: "POST", body: body ?? {}, headers }),
};
