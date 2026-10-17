import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { StrikeRow } from "../src/api/types";
import { StrangleChart, summariseBars, symmetricPair } from "../src/components/StrangleChart";

vi.mock("../src/components/CandleChart", () => ({
  CandleChart: ({ series, summary }: { series: unknown[]; summary: string }) => <div role="img" aria-label={summary} data-bars={series.length} />,
  describeSeries: (name: string, bars: unknown[]) => `${name}: ${bars.length} candles`,
}));

const json = (status: number, body: unknown) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
const rows = [24750, 24800, 24850, 24900, 24950, 25000, 25050].map((k) => ({ strike: k, call_ltp: 100 + (24850 - k) / 10, put_ltp: 100 - (24850 - k) / 10, is_atm: k === 24850 }) as StrikeRow);
const bar = (o: number, h: number, l: number, c: number, time = 1) => ({ time, open: o, high: h, low: l, close: c });
const reply = (ce: number, pe: number, bars = [bar(150, 170, 140, 160)]) => ({
  ce_strike: ce, pe_strike: pe, expiry: "22-Oct-2026", tracked_since: "2026-10-19T10:15:00+05:30", by_interval: { "1": { pair: bars }, "5": { pair: bars }, "15": { pair: bars } },
});

let urls: string[];
let status = 200;
beforeEach(() => {
  urls = [];
  status = 200;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      urls.push(url);
      if (status === 404) return json(404, { detail: "Both strikes must be in the live option chain" });
      const q = new URL(url, "http://x");
      return json(200, reply(Number(q.searchParams.get("ce_strike")), Number(q.searchParams.get("pe_strike"))));
    }),
  );
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("helpers", () => {
  it("picks strikes the same number of steps either side of the money", () => {
    expect(symmetricPair(rows, 24850, 0)).toEqual({ ce: 24850, pe: 24850 });
    expect(symmetricPair(rows, 24850, 2)).toEqual({ ce: 24950, pe: 24750 });
  });
  it("is null rather than a wrong pair when the chain is too short or has no money strike", () => {
    expect(symmetricPair(rows, 24850, 4)).toBeNull();
    expect(symmetricPair(rows, null, 1)).toBeNull();
    expect(symmetricPair(rows, 99999, 1)).toBeNull();
  });
  it("summarises open, high, low, close and percent change from the bars", () => {
    expect(summariseBars([bar(100, 120, 90, 110), bar(110, 130, 105, 115)])).toEqual({ open: 100, close: 115, high: 130, low: 90, changePct: 15 });
    expect(summariseBars([])).toBeNull();
  });
});

describe("StrangleChart", () => {
  it("starts from a +-2 strangle, titles it as one, and requests exactly that pair", async () => {
    render(<StrangleChart rows={rows} atm={24850} expiry="22-Oct-2026" />);
    await waitFor(() => expect(urls.length).toBeGreaterThan(0));
    expect(urls[0]).toBe("/api/pair-candles?ce_strike=24950&pe_strike=24750");
    expect(screen.getByRole("heading", { name: /Strangle · 24,950 CE \+ 24,750 PE/ })).toBeTruthy();
    expect(await screen.findByRole("img", { name: /Strangle of 24,950 call and 24,750 put/ })).toBeTruthy();
  });

  it("shows the live combined premium from the chain, and the day's range from the candles", async () => {
    render(<StrangleChart rows={rows} atm={24850} expiry={null} />);
    // 24950 CE = 90, 24750 PE = 90 -> 180
    await waitFor(() => expect(screen.getByText("180.00")).toBeTruthy());
    expect(await screen.findByText(/Open 150\.00 · High 170\.00 · Low 140\.00/)).toBeTruthy();
  });

  it("the ATM quick pair makes it a straddle", async () => {
    render(<StrangleChart rows={rows} atm={24850} expiry={null} />);
    await screen.findByRole("img");
    fireEvent.click(screen.getByRole("button", { name: "ATM straddle" }));
    expect(await screen.findByRole("heading", { name: /Straddle · 24,850 CE \+ 24,850 PE/ })).toBeTruthy();
    await waitFor(() => expect(urls.at(-1)).toBe("/api/pair-candles?ce_strike=24850&pe_strike=24850"));
  });

  it("choosing strikes by hand requests that pair and sticks even as the money strike moves", async () => {
    const { rerender } = render(<StrangleChart rows={rows} atm={24850} expiry={null} />);
    await screen.findByRole("img");
    fireEvent.change(screen.getByLabelText("Call strike"), { target: { value: "25050" } });
    fireEvent.change(screen.getByLabelText("Put strike"), { target: { value: "24800" } });
    await waitFor(() => expect(urls.at(-1)).toBe("/api/pair-candles?ce_strike=25050&pe_strike=24800"));
    rerender(<StrangleChart rows={rows} atm={24900} expiry={null} />); // ATM moved
    expect((screen.getByLabelText("Call strike") as HTMLSelectElement).value).toBe("25050");
    expect(screen.getByRole("heading", { name: /25,050 CE \+ 24,800 PE/ })).toBeTruthy();
  });

  it("says when tracking began and that earlier prices do not exist", async () => {
    render(<StrangleChart rows={rows} atm={24850} expiry={null} />);
    expect(await screen.findByText(/Tracking since 10:15/)).toBeTruthy();
    expect(screen.getByText(/Earlier prices of this pair were not recorded/)).toBeTruthy();
  });

  it("explains instead of charting when the server does not have the strikes", async () => {
    status = 404;
    render(<StrangleChart rows={rows} atm={24850} expiry={null} />);
    expect(await screen.findByText("These strikes are not in the live chain")).toBeTruthy();
  });

  it("waits for the chain before offering anything", () => {
    render(<StrangleChart rows={[]} atm={null} expiry={null} />);
    expect(screen.getByText("Waiting for the option chain")).toBeTruthy();
    expect(urls).toEqual([]);
  });
});
