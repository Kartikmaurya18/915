/**
 * End-to-end checks of the browser code paths against a REAL running backend
 * (AUTH_MODE=required, mock market data):
 *
 *     E2E_BASE_URL=http://127.0.0.1:8010 npm run test:e2e
 *
 * It uses the production `request()` wrapper and `RealtimeClient` unchanged; only `fetch` is wrapped
 * to make relative URLs absolute and to keep the refresh cookie, which a browser would do for us.
 */
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { ApiError, request, setAccessToken, getAccessToken, type TokenResponse } from "../../src/api/http";
import type { OptionsTopic, PaperPosition, PaperState } from "../../src/api/types";
import { RealtimeClient } from "../../src/realtime/client";

const BASE = process.env.E2E_BASE_URL;
const suite = BASE ? describe : describe.skip;
const WS = (BASE ?? "").replace(/^http/, "ws") + "/ws/v1";

let cookie = "";
const realFetch = globalThis.fetch;

beforeAll(async () => {
  if (!BASE) return;
  globalThis.fetch = (async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const headers = new Headers(init.headers);
    if (cookie) headers.set("Cookie", cookie);
    const res = await realFetch(new URL(String(input), BASE), { ...init, headers });
    const set = res.headers.getSetCookie?.() ?? [];
    for (const c of set) {
      const [pair] = c.split(";");
      if (/Max-Age=0|expires=Thu, 01 Jan 1970/i.test(c)) cookie = "";
      else if (pair) cookie = pair;
    }
    return res;
  }) as typeof fetch;
  users.a = await register();
  users.b = await register();
});
afterAll(() => {
  globalThis.fetch = realFetch;
});

const email = () => `e2e-${Date.now()}-${Math.random().toString(36).slice(2, 8)}@example.com`;
const PASSWORD = "e2e-correct-horse-battery";

// The server allows 10 registrations per hour per IP, so the run registers two users once and every
// test signs in as one of them (each login starts a fresh refresh-token family).
const users: { a: string; b: string } = { a: "", b: "" };

async function register(): Promise<string> {
  cookie = "";
  const addr = email();
  await request<TokenResponse>("/api/auth/register", { method: "POST", body: { email: addr, password: PASSWORD }, credentials: true, noRefresh: true });
  return addr;
}

async function signIn(addr: string): Promise<TokenResponse> {
  cookie = "";
  const res = await request<TokenResponse>("/api/auth/login", { method: "POST", body: { email: addr, password: PASSWORD }, credentials: true, noRefresh: true });
  setAccessToken(res.access_token);
  return res;
}

const signUp = () => signIn(users.a);

function newClient(token: () => string | null = getAccessToken, onAuthFailure?: () => Promise<boolean>): RealtimeClient {
  return new RealtimeClient({ url: () => WS, authRequired: () => true, getToken: token, onAuthFailure });
}

function until<T>(check: () => T | undefined | false, ms = 15_000, step = 50): Promise<T> {
  return new Promise((resolve, reject) => {
    const started = Date.now();
    const timer = setInterval(() => {
      const v = check();
      if (v) {
        clearInterval(timer);
        resolve(v as T);
      } else if (Date.now() - started > ms) {
        clearInterval(timer);
        reject(new Error(`timed out after ${ms} ms`));
      }
    }, step);
  });
}

suite("auth against the live API", () => {
  it("registers, reads /me, and rejects a wrong password with the same message as an unknown user", async () => {
    const session = await signUp();
    expect(session.user.role).toBe("user");
    const me = await request<{ email: string }>("/api/auth/me");
    expect(me.email).toBe(session.user.email);

    const wrong = await request("/api/auth/login", { method: "POST", body: { email: session.user.email, password: "not-the-password" }, credentials: true, noRefresh: true }).catch((e) => e);
    const unknown = await request("/api/auth/login", { method: "POST", body: { email: email(), password: "not-the-password" }, credentials: true, noRefresh: true }).catch((e) => e);
    expect(wrong).toBeInstanceOf(ApiError);
    expect((wrong as ApiError).message).toBe((unknown as ApiError).message);
  });

  it("rotates the refresh cookie and kills the session when an old one is replayed", async () => {
    await signUp();
    const first = cookie;
    expect(first).toContain("refresh_token=");
    const rotated = await request<TokenResponse>("/api/auth/refresh", { method: "POST", body: {}, credentials: true, noRefresh: true });
    expect(rotated.access_token).toBeTruthy();
    const second = cookie;
    expect(second).not.toBe(first);

    cookie = first; // a thief replays the spent token
    const replay = await request("/api/auth/refresh", { method: "POST", body: {}, credentials: true, noRefresh: true }).catch((e) => e);
    expect((replay as ApiError).code).toBe("refresh_token_reused");
    cookie = second; // the legitimate, newer token is now dead too (whole family revoked)
    const after = await request("/api/auth/refresh", { method: "POST", body: {}, credentials: true, noRefresh: true }).catch((e) => e);
    expect((after as ApiError).status).toBe(401);
  });

  it("refuses refresh without the CSRF header", async () => {
    await signUp();
    const res = await realFetch(new URL("/api/auth/refresh", BASE), { method: "POST", headers: { Cookie: cookie } });
    expect(res.status).toBe(403);
  });

  it("transparently refreshes an expired access token inside request()", async () => {
    await signUp();
    const { setRefreshHandler } = await import("../../src/api/http");
    setRefreshHandler(async () => {
      const r = await request<TokenResponse>("/api/auth/refresh", { method: "POST", body: {}, credentials: true, noRefresh: true });
      setAccessToken(r.access_token);
      return true;
    });
    setAccessToken("expired.or.garbage");
    const me = await request<{ id: string }>("/api/auth/me");
    expect(me.id).toBeTruthy();
    setRefreshHandler(null);
  });
});

suite("realtime against the live gateway", () => {
  it("streams snapshots and patches that keep the client's state identical to the server's", async () => {
    await signUp();
    const a = newClient();
    const b = newClient();
    const topics = ["index:NIFTY", "options:NIFTY", "candles:NIFTY:1m"];
    for (const t of topics) {
      a.subscribe(t, () => undefined);
    }
    a.start();
    await until(() => topics.every((t) => a.getTopic(t)));

    // Let several ticks arrive; at least some must be patches (not only snapshots).
    await until(() => a.getInfo().patches >= 4, 30_000);
    expect(a.getInfo().resyncs).toBe(0);

    // A second client that connects later gets full snapshots straight from the server. Wherever both
    // hold the same (epoch, seq), the patched state of `a` must equal the server's snapshot for `b`.
    for (const t of topics) b.subscribe(t, () => undefined);
    b.start();
    await until(() => topics.every((t) => b.getTopic(t)));
    let compared = 0;
    await until(() => {
      for (const t of topics) {
        const sa = a.getTopic(t);
        const sb = b.getTopic(t);
        if (sa && sb && sa.epoch === sb.epoch && sa.seq === sb.seq) {
          expect(sa.data).toEqual(sb.data);
          compared++;
        }
      }
      return compared >= topics.length;
    }, 30_000, 20);

    expect(a.getInfo().feedAgeMs === null || a.getInfo().feedAgeMs! >= 0).toBe(true);
    a.stop();
    b.stop();
  });

  it("delivers a well-formed option chain", async () => {
    await signUp();
    const c = newClient();
    c.subscribe("options:NIFTY", () => undefined);
    c.start();
    const chain = (await until(() => c.getTopic("options:NIFTY")?.data)) as unknown as OptionsTopic;
    expect(chain.strikes.length).toBeGreaterThan(5);
    expect(chain.strikes.filter((s) => s.is_atm)).toHaveLength(1);
    c.stop();
  });

  it("rejects a bad token and reports auth_failed instead of retrying forever", async () => {
    const c = newClient(() => "not-a-real-token", async () => false);
    c.subscribe("index:NIFTY", () => undefined);
    c.start();
    await until(() => c.getInfo().status === "auth_failed");
    expect(c.getTopic("index:NIFTY")).toBeUndefined();
    c.stop();
  });

  it("recovers after the server drops the connection", async () => {
    await signUp();
    const c = newClient();
    c.subscribe("index:NIFTY", () => undefined);
    c.start();
    await until(() => c.getTopic("index:NIFTY"));
    // Simulate a network drop from the client side by closing the underlying socket.
    (c as unknown as { ws: WebSocket }).ws.close(4000, "test_drop");
    await until(() => c.getInfo().status === "reconnecting" || c.getInfo().reconnects > 0);
    await until(() => c.getInfo().status === "live" && c.getTopic("index:NIFTY"));
    expect(c.getInfo().reconnects).toBeGreaterThanOrEqual(1);
    c.stop();
  });

  it("keeps the socket open across a token renewal", async () => {
    await signUp();
    const c = newClient();
    c.subscribe("index:NIFTY", () => undefined);
    c.start();
    await until(() => c.getTopic("index:NIFTY"));
    const refreshed = await request<TokenResponse>("/api/auth/refresh", { method: "POST", body: {}, credentials: true, noRefresh: true });
    setAccessToken(refreshed.access_token);
    const before = c.getInfo().reconnects;
    c.renewToken();
    await new Promise((r) => setTimeout(r, 1500));
    expect(c.getInfo().status).toBe("live");
    expect(c.getInfo().reconnects).toBe(before);
    c.stop();
  });
});

suite("paper trading against the live API", () => {
  it("opens once per Idempotency-Key, lists it, closes it, and keeps users isolated", async () => {
    await signUp();
    // Wait for a priced chain on the server (the first tick can take a moment).
    const key = crypto.randomUUID();
    const body = { option_type: "CE", side: "BUY", quantity: 50, strike: null };
    const open = async () => {
      const deadline = Date.now() + 20_000;
      for (;;) {
        try {
          return await request<PaperPosition>("/api/paper/open", { method: "POST", body, headers: { "Idempotency-Key": key } });
        } catch (err) {
          if (!(err instanceof ApiError) || err.status !== 400 || Date.now() > deadline) throw err;
          await new Promise((r) => setTimeout(r, 500)); // chain not available yet
        }
      }
    };
    const first = await open();
    const again = await request<PaperPosition>("/api/paper/open", { method: "POST", body, headers: { "Idempotency-Key": key } });
    expect(again.id).toBe(first.id); // the retry did not open a second position
    expect(first.status).toBe("OPEN");

    const state = await request<PaperState>("/api/paper/state");
    expect(state.open_positions.map((p) => p.id)).toEqual([first.id]);

    // A different user sees nothing and cannot close it.
    const owner = getAccessToken();
    await signIn(users.b);
    const other = await request<PaperState>("/api/paper/state");
    expect(other.open_positions).toHaveLength(0);
    const stolen = await request("/api/paper/close/" + first.id, { method: "POST", body: {} }).catch((e) => e);
    expect((stolen as ApiError).status).toBe(404);

    // The owner closes it; it moves to history.
    setAccessToken(owner);
    const closed = await request<PaperPosition>("/api/paper/close/" + first.id, { method: "POST", body: {} });
    expect(closed.status).toBe("EXITED");
    const after = await request<PaperState>("/api/paper/state");
    expect(after.open_positions).toHaveLength(0);
    expect(after.history.map((p) => p.id)).toContain(first.id);

    // Closing twice reports "not found / already closed", it does not double-count.
    const twice = await request("/api/paper/close/" + first.id, { method: "POST", body: {} }).catch((e) => e);
    expect((twice as ApiError).status).toBe(404);
  });

  it("returns validation errors a user can read", async () => {
    await signUp();
    const err = await request("/api/paper/open", { method: "POST", body: { option_type: "CE", side: "BUY", quantity: 0 } }).catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).code).toBe("validation");
    expect((err as ApiError).message).not.toBe("Request failed");
  });

  it("blocks the operator endpoints for a normal user", async () => {
    await signUp();
    for (const path of ["/api/logs", "/api/settings/atm"]) {
      const err = await request(path, path.includes("settings") ? { method: "POST", body: {} } : {}).catch((e) => e);
      expect((err as ApiError).status).toBe(403);
    }
  });
});

suite("straddle chart and history against the live API", () => {
  it("serves CE/PE/Straddle candles for the ATM strike and answers repeated requests consistently", async () => {
    await signUp();
    const c = newClient();
    c.subscribe("index:NIFTY", () => undefined);
    c.start();
    const index = (await until(() => c.getTopic("index:NIFTY")?.data)) as unknown as { atm: { strike: number } };
    c.stop();
    const strike = index.atm.strike;
    const get = () => request<{ strike: number; by_interval: Record<string, Record<string, { time: number; open: number; high: number; low: number; close: number }[]>> }>(`/api/straddle-candles?strike=${strike}`);
    const deadline = Date.now() + 15_000;
    let first = await get().catch((e) => e as ApiError);
    while (first instanceof ApiError && first.status === 404 && Date.now() < deadline) {
      await new Promise((r) => setTimeout(r, 500));
      first = await get().catch((e) => e as ApiError);
    }
    expect(first).not.toBeInstanceOf(ApiError);
    const data = first as Awaited<ReturnType<typeof get>>;
    expect(data.strike).toBe(strike);
    for (const interval of ["1", "5", "15"]) {
      for (const leg of ["ce", "pe", "straddle"]) {
        const series = data.by_interval[interval]?.[leg];
        expect(series?.length, `${interval}m ${leg}`).toBeGreaterThan(0);
        for (const b of series!) expect(b.high).toBeGreaterThanOrEqual(b.low);
      }
    }
    // Straddle = CE + PE on the latest bar of the 1m series (closing prices add up).
    const last = (leg: string) => data.by_interval["1"]![leg]!.at(-1)!;
    expect(last("straddle").close).toBeCloseTo(last("ce").close + last("pe").close, 1);
  });

  it("returns a readable 404 for a strike that has no candles", async () => {
    await signUp();
    const err = await request("/api/straddle-candles?strike=1").catch((e) => e);
    expect((err as ApiError).status).toBe(404);
  });

  it("lists trading dates and returns a coherent past session", async () => {
    await signUp();
    const dates = await request<{ dates: string[]; today: string }>("/api/history/dates");
    expect(dates.today).toMatch(/^\d{4}-\d{2}-\d{2}$/);
    const past = dates.dates.find((d) => d < dates.today);
    if (!past) return; // a fresh database has no past sessions; nothing more to check
    const s = await request<{ has_data: boolean; candles: Record<string, unknown[]>; available_strikes: number[]; underlying: { high: number; low: number } }>(`/api/history/session?date=${past}`);
    expect(s.has_data).toBe(true);
    expect(s.underlying.high).toBeGreaterThanOrEqual(s.underlying.low);
    expect(Object.keys(s.candles).length).toBeGreaterThan(0);
    if (s.available_strikes.length) {
      const strike = s.available_strikes[0]!;
      const day = await request<{ strike: number }>(`/api/straddle-candles?strike=${strike}&date=${past}`);
      expect(day.strike).toBe(strike);
    }
  });

  it("rejects a malformed date", async () => {
    await signUp();
    const err = await request("/api/history/session?date=16-10-2026").catch((e) => e);
    expect((err as ApiError).status).toBe(400);
  });
});

suite("paper-change notices against the live server", () => {
  it("tells the owner's open socket (and only theirs) when their book changes", async () => {
    await signIn(users.a);
    const a = newClient();
    a.subscribe("index:NIFTY", () => undefined);
    let notices = 0;
    a.onPaperChanged(() => notices++);
    a.start();
    await until(() => a.getTopic("index:NIFTY"));
    const tokenA = getAccessToken();

    await signIn(users.b);
    const b = newClient();
    b.subscribe("index:NIFTY", () => undefined);
    let bNotices = 0;
    b.onPaperChanged(() => bNotices++);
    b.start();
    await until(() => b.getTopic("index:NIFTY"));

    setAccessToken(tokenA); // act as user A over REST
    const open = async () => {
      const deadline = Date.now() + 20_000;
      for (;;) {
        try {
          return await request<PaperPosition>("/api/paper/open", { method: "POST", body: { option_type: "CE", side: "BUY", quantity: 10, strike: null } });
        } catch (err) {
          if (!(err instanceof ApiError) || err.status !== 400 || Date.now() > deadline) throw err;
          await new Promise((r) => setTimeout(r, 500));
        }
      }
    };
    const position = await open();
    await until(() => notices >= 1, 5000); // pushed within moments, no polling involved
    const afterOpen = notices;
    await request("/api/paper/close/" + position.id, { method: "POST", body: {} });
    await until(() => notices > afterOpen, 5000);
    expect(bNotices).toBe(0); // user B heard nothing about A's trades
    a.stop();
    b.stop();
  });
});
