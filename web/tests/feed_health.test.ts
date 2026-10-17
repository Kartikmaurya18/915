import { describe, expect, it } from "vitest";
import { CHAIN_STALE_SECONDS, TOKEN_WARN_MINUTES, connectionMessage, dataMessages, healthMessages } from "../src/lib/feedHealth";
import type { IndexTopic } from "../src/api/types";

const NOW = Date.parse("2026-10-19T11:00:00+05:30");

function index(over: { simulated?: boolean; status?: string; detail?: string | null; session?: string; open?: boolean; chainAge?: number | null; expires?: string | null } = {}): IndexTopic {
  return {
    underlying: "NIFTY",
    price: 24850,
    provider: { name: over.simulated === false ? "DHAN" : "MOCK", status: over.status ?? "LIVE", status_detail: over.detail ?? null, is_simulated: over.simulated ?? true, underlying: "NIFTY", credentials_expire_at: over.expires ?? null },
    data_quality: { market_session: over.session ?? "OPEN", market_open: over.open ?? true, chain_age_seconds: over.chainAge ?? 1 },
  } as unknown as IndexTopic;
}
const real = (o: Parameters<typeof index>[0] = {}) => index({ simulated: false, ...o });

describe("connection messages", () => {
  it("is silent while live, connecting or signed out (other parts of the UI handle those)", () => {
    for (const s of ["live", "connecting", "signed-out"] as const) expect(connectionMessage(s, 500)).toBeNull();
  });
  it("warns when stale, reconnecting or offline", () => {
    expect(connectionMessage("stale", 42_000)!.text).toMatch(/42\.0 s|42 s/);
    expect(connectionMessage("reconnecting", null)!.text).toMatch(/reconnecting/);
    expect(connectionMessage("offline", null)!.text).toMatch(/offline/);
    for (const s of ["stale", "reconnecting", "offline"] as const) expect(connectionMessage(s, 1000)!.tone).toBe("warn");
  });
});

describe("data messages", () => {
  it("says nothing before the first snapshot", () => {
    expect(dataMessages(undefined, NOW)).toEqual([]);
  });
  it("says nothing about a healthy simulated feed, however late the hour", () => {
    expect(dataMessages(index({ session: "CLOSED", open: false }), NOW)).toEqual([]); // mock ticks around the clock by design
  });
  it("says nothing about a healthy real feed during the session", () => {
    expect(dataMessages(real(), NOW)).toEqual([]);
  });
  it("explains a closed real market, by reason, as information not an alarm", () => {
    for (const [session, text] of [["WEEKEND", /weekend/], ["HOLIDAY", /holiday/], ["PRE_OPEN", /not opened/], ["CLOSED", /closed for the day/]] as const) {
      const [m] = dataMessages(real({ session, open: false }), NOW);
      expect(m!.id).toBe("market-closed");
      expect(m!.tone).toBe("info");
      expect(m!.text).toMatch(text);
      expect(m!.text).toMatch(/last session/);
    }
  });
  it("flags an option chain that stopped refreshing while the market is open", () => {
    const [m] = dataMessages(real({ chainAge: CHAIN_STALE_SECONDS + 15 }), NOW);
    expect(m).toMatchObject({ id: "chain-stale", tone: "warn" });
    expect(m!.text).toMatch(/45\.0 s|45 s/);
    expect(dataMessages(real({ chainAge: CHAIN_STALE_SECONDS }), NOW)).toEqual([]); // at the limit is still fine
    expect(dataMessages(real({ chainAge: null }), NOW)).toEqual([]);
  });
  it("does not call the chain stale when the market is simply closed", () => {
    const ids = dataMessages(real({ session: "CLOSED", open: false, chainAge: 9999 }), NOW).map((m) => m.id);
    expect(ids).toEqual(["market-closed"]);
  });
  it("surfaces a provider error with its reason, for real and simulated feeds alike", () => {
    for (const simulated of [true, false]) {
      const [m] = dataMessages(index({ simulated, status: "ERROR", detail: "Dhan access token has expired (807)" }), NOW);
      expect(m).toMatchObject({ id: "provider-error", tone: "warn" });
      expect(m!.text).toContain("Dhan access token has expired (807)");
    }
    expect(dataMessages(index({ status: "ERROR", detail: null }), NOW)[0]!.text).toMatch(/reports a problem\./);
  });
});

describe("access token expiry", () => {
  const inMinutes = (m: number) => new Date(NOW + m * 60_000).toISOString();
  it("warns inside the last hour, naming the time and the minutes left", () => {
    const [m] = dataMessages(real({ expires: inMinutes(38) }), NOW);
    expect(m).toMatchObject({ id: "token", tone: "warn" });
    expect(m!.text).toMatch(/in 38 min/);
    expect(m!.text).toMatch(/11:38 IST/);
  });
  it("is quiet when there is plenty of time, or no expiry is known", () => {
    expect(dataMessages(real({ expires: inMinutes(TOKEN_WARN_MINUTES + 30) }), NOW)).toEqual([]);
    expect(dataMessages(real({ expires: null }), NOW)).toEqual([]);
    expect(dataMessages(real({ expires: "not a date" }), NOW)).toEqual([]);
  });
  it("says so plainly once it has expired", () => {
    expect(dataMessages(real({ expires: inMinutes(-5) }), NOW)[0]!.text).toMatch(/has expired/);
    expect(dataMessages(real({ expires: inMinutes(0) }), NOW)[0]!.text).toMatch(/has expired/);
  });
  it("does not nag a simulated feed about a token it does not use", () => {
    expect(dataMessages(index({ expires: inMinutes(5) }), NOW)).toEqual([]);
  });
});

describe("combined", () => {
  it("puts the connection problem first, then data problems", () => {
    const msgs = healthMessages("stale", 40_000, real({ status: "ERROR", detail: "x", chainAge: 90 }), NOW);
    expect(msgs.map((m) => m.id)).toEqual(["connection", "provider-error", "chain-stale"]);
  });
  it("is empty when everything is healthy", () => {
    expect(healthMessages("live", 300, real(), NOW)).toEqual([]);
  });
});
