import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import History from "../src/pages/History";

// The chart library needs a real canvas; what matters here is the data handed to it.
vi.mock("../src/components/CandleChart", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/components/CandleChart")>()),
  CandleChart: ({ series, resetKey }: { series: unknown[]; resetKey: string }) => <div data-testid="chart" data-key={resetKey} data-bars={series.length} />,
}));

const json = (status: number, body: unknown) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
const candle = (t: number) => ({ symbol: "NIFTY", interval_minutes: 1, open_time: "2026-10-15T09:15:00+05:30", close_time: "x", time: t, open: 1, high: 2, low: 0.5, close: 1.5, tick_count: 1, is_complete: true });
const bars = (n: number) => Array.from({ length: n }, (_, i) => ({ time: 1000 + i, open: 1, high: 2, low: 0.5, close: 1.5 }));

const session = (date: string, extra: Record<string, unknown> = {}) => ({
  trading_date: date,
  has_data: true,
  underlying: { symbol: "NIFTY", open: 24700, high: 24900, low: 24650, close: 24850, last_updated: date + "T15:30:00+05:30" },
  candles: { "1": [candle(1), candle(2), candle(3)], "5": [candle(1)] },
  option_chain_snapshots: [{ timestamp: date + "T15:29:00+05:30", expiry: "22-Oct-2026", atm_strike: 24850, atm_call_ltp: 100, atm_put_ltp: 90, straddle_premium: 190 }],
  available_strikes: [24800, 24850],
  straddle_candles: { strike: 24850, by_interval: { "1": { straddle: bars(4), ce: bars(4), pe: bars(4) } } },
  paper_trading: { history: [], stats: { total_trades: 0, total_pnl: 0 }, pnl_summary: {} },
  ...extra,
});

const DATES = { dates: ["2026-10-15", "2026-09-17", "2026-09-11"], today: "2026-10-16" };
let requested: string[];

function mockApi(sessions: Record<string, unknown>) {
  requested = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      requested.push(url);
      if (url.startsWith("/api/history/dates")) return json(200, DATES);
      const m = /\/api\/history\/session\?date=([\d-]+)/.exec(url);
      if (m) return sessions[m[1]!] ? json(200, sessions[m[1]!]) : json(200, { ...session(m[1]!), has_data: false });
      if (url.startsWith("/api/straddle-candles")) return json(200, { strike: 24800, by_interval: { "1": { straddle: bars(7), ce: bars(7), pe: bars(7) } } });
      return json(404, { detail: "unmocked " + url });
    }),
  );
}

const view = () => render(<MemoryRouter><History /></MemoryRouter>);
const dateInput = () => screen.getByLabelText("Trading date") as HTMLInputElement;

beforeEach(() => localStorage.clear());
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("History page", () => {
  it("opens on the most recent day with data and shows the session", async () => {
    mockApi({ "2026-10-15": session("2026-10-15") });
    view();
    await waitFor(() => expect(dateInput().value).toBe("2026-10-15"));
    expect(await screen.findByText("24,850.00")).toBeTruthy(); // close
    expect(screen.getByText("15-10-2026")).toBeTruthy();
    const charts = screen.getAllByTestId("chart");
    expect(charts[0]!.getAttribute("data-bars")).toBe("3"); // 1m candles
    expect(charts[1]!.getAttribute("data-bars")).toBe("4"); // straddle taken from the bundle: no extra request
    expect(requested.some((u) => u.startsWith("/api/straddle-candles"))).toBe(false);
  });

  it("Previous skips days without data", async () => {
    mockApi({ "2026-10-15": session("2026-10-15"), "2026-09-17": session("2026-09-17") });
    view();
    await waitFor(() => expect(dateInput().value).toBe("2026-10-15"));
    fireEvent.click(screen.getByLabelText("Previous trading day"));
    await waitFor(() => expect(dateInput().value).toBe("2026-09-17")); // not 2026-10-14
    fireEvent.click(screen.getByLabelText("Previous trading day"));
    await waitFor(() => expect(dateInput().value).toBe("2026-09-11"));
    await waitFor(() => expect((screen.getByLabelText("Previous trading day") as HTMLButtonElement).disabled).toBe(true));
  });

  it("Next reaches today and then stops", async () => {
    mockApi({ "2026-10-15": session("2026-10-15"), "2026-10-16": session("2026-10-16") });
    view();
    await waitFor(() => expect(dateInput().value).toBe("2026-10-15"));
    fireEvent.click(screen.getByLabelText("Next trading day"));
    await waitFor(() => expect(dateInput().value).toBe("2026-10-16"));
    await waitFor(() => expect((screen.getByLabelText("Next trading day") as HTMLButtonElement).disabled).toBe(true));
    expect(screen.getByText("Open the live view →")).toBeTruthy();
  });

  it("a picked date without data snaps to the nearest earlier day that has data", async () => {
    mockApi({ "2026-09-17": session("2026-09-17") });
    view();
    await waitFor(() => expect(dateInput().value).toBe("2026-10-15"));
    fireEvent.change(dateInput(), { target: { value: "2026-10-01" } });
    await waitFor(() => expect(dateInput().value).toBe("2026-09-17"));
  });

  it("says so when a session has no data", async () => {
    mockApi({});
    view();
    expect(await screen.findByText("No data available for this trading session.")).toBeTruthy();
  });

  it("choosing another strike loads that strike's candles for the same date", async () => {
    mockApi({ "2026-10-15": session("2026-10-15") });
    view();
    await screen.findByText("24,850.00");
    fireEvent.change(screen.getByLabelText("Strike"), { target: { value: "24800" } });
    await waitFor(() => expect(requested.some((u) => u === "/api/straddle-candles?strike=24800&date=2026-10-15")).toBe(true));
    await waitFor(() => expect(screen.getAllByTestId("chart")[1]!.getAttribute("data-bars")).toBe("7"));
  });

  it("ignores a slow answer for a date the user has already left", async () => {
    let releaseFirst: (r: Response) => void = () => undefined;
    requested = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        requested.push(url);
        if (url.startsWith("/api/history/dates")) return json(200, DATES);
        if (url.includes("date=2026-10-15")) return new Promise<Response>((r) => (releaseFirst = r)); // slow
        if (url.includes("date=2026-09-17")) return json(200, session("2026-09-17", { underlying: { symbol: "NIFTY", open: 1, high: 2, low: 0, close: 777, last_updated: null } }));
        return json(404, {});
      }),
    );
    view();
    await waitFor(() => expect(requested.some((u) => u.includes("date=2026-10-15"))).toBe(true));
    fireEvent.change(dateInput(), { target: { value: "2026-09-17" } });
    await screen.findByText("777.00");
    releaseFirst(json(200, session("2026-10-15", { underlying: { symbol: "NIFTY", open: 1, high: 2, low: 0, close: 555, last_updated: null } })));
    await new Promise((r) => setTimeout(r, 30));
    expect(screen.queryByText("555.00")).toBeNull();
    expect(screen.getByText("777.00")).toBeTruthy();
  });
});
