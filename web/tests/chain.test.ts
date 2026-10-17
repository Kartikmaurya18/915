import { describe, expect, it } from "vitest";
import { COLUMNS, DEFAULT_SORT, cellValue, filterRows, moneyness, nextSort, orderedColumns, sortRows } from "../src/lib/chain";
import type { StrikeRow } from "../src/api/types";

const row = (strike: number, o: Partial<StrikeRow> = {}): StrikeRow =>
  ({
    strike, call_ltp: 100, put_ltp: 100, call_oi: 10, put_oi: 10, call_volume: 1, put_volume: 1, call_delta: 0.5, put_delta: -0.5,
    call_iv: 14, put_iv: 14, call_bid: 99, call_ask: 101, put_bid: 99, put_ask: 101, call_gamma: 0.001, put_gamma: 0.001,
    call_theta: -16, put_theta: -16, call_vega: 11, put_vega: 11, call_oi_change: 5, put_oi_change: -5, combined_value: 200, is_atm: false, ...o,
  }) as StrikeRow;
const chain = [24700, 24750, 24800, 24850, 24900, 24950, 25000].map((k) => row(k, { is_atm: k === 24850 }));

describe("moneyness", () => {
  it("calls are in the money below the underlying, puts above it", () => {
    expect(moneyness(24800, 24850, "call", false)).toBe("ITM");
    expect(moneyness(24900, 24850, "call", false)).toBe("OTM");
    expect(moneyness(24900, 24850, "put", false)).toBe("ITM");
    expect(moneyness(24800, 24850, "put", false)).toBe("OTM");
  });
  it("the ATM strike is ATM whichever side, and unknown spot is never called in the money", () => {
    expect(moneyness(24850, 24851, "call", true)).toBe("ATM");
    expect(moneyness(24800, null, "call", false)).toBe("OTM");
  });
});

describe("columns", () => {
  it("mirror around the strike so LTP always sits next to it", () => {
    const visible = ["ltp", "delta", "oi"] as const;
    expect(orderedColumns(visible, "put").map((c) => c.key)).toEqual(["ltp", "delta", "oi"]);
    expect(orderedColumns(visible, "call").map((c) => c.key)).toEqual(["oi", "delta", "ltp"]);
  });
  it("covers every Greek, bid/ask and OI change, each with a real field behind it", () => {
    const r = row(24850);
    for (const c of COLUMNS) {
      expect(cellValue(r, "call", c.key), c.key).not.toBeNull();
      expect(cellValue(r, "put", c.key), c.key).not.toBeNull();
    }
  });
  it("formats missing values as a dash, never as zero", () => {
    expect(COLUMNS.every((c) => c.format(null) === "—")).toBe(true);
    expect(COLUMNS.find((c) => c.key === "oichg")!.format(1500)).toMatch(/^\+/);
    expect(COLUMNS.find((c) => c.key === "oichg")!.format(-1500)).toMatch(/^−/);
    expect(COLUMNS.find((c) => c.key === "iv")!.format(13.456)).toBe("13.5%");
  });
});

describe("filtering", () => {
  it("windows the chain around the ATM strike", () => {
    expect(filterRows(chain, { window: 2, atm: 24850, query: "" }).map((r) => r.strike)).toEqual([24750, 24800, 24850, 24900, 24950]);
  });
  it("clips at the edges instead of failing", () => {
    expect(filterRows(chain, { window: 6, atm: 24850, query: "" })).toHaveLength(7);
    const noFlag = chain.map((r) => ({ ...r, is_atm: false }));
    expect(filterRows(noFlag, { window: 3, atm: 24700, query: "" }).map((r) => r.strike)).toEqual([24700, 24750, 24800, 24850]);
  });
  it("shows everything for 'all', and when there is no ATM yet", () => {
    expect(filterRows(chain, { window: "all", atm: 24850, query: "" })).toHaveLength(7);
    expect(filterRows(chain, { window: 3, atm: null, query: "" })).toHaveLength(7);
  });
  it("a typed strike overrides the window and ignores non-digits", () => {
    expect(filterRows(chain, { window: 3, atm: 24850, query: "250" }).map((r) => r.strike)).toEqual([25000]); // outside the window, still found
    expect(filterRows(chain, { window: 3, atm: 24850, query: "999" })).toEqual([]);
    expect(filterRows(chain, { window: 3, atm: 24850, query: "24,9" }).map((r) => r.strike)).toEqual([24900, 24950]);
    expect(filterRows(chain, { window: 3, atm: 24850, query: "25000" }).map((r) => r.strike)).toEqual([25000]);
  });
  it("sorts by strike regardless of the order the server sent", () => {
    expect(filterRows([...chain].reverse(), { window: "all", atm: null, query: "" })[0]!.strike).toBe(24700);
  });
});

describe("sorting", () => {
  const varied = [row(1, { call_oi: 30 }), row(2, { call_oi: null }), row(3, { call_oi: 10 }), row(4, { call_oi: 20 })];
  it("orders by a column, highest first by default", () => {
    expect(sortRows(varied, { key: "call:oi", dir: "desc" }).map((r) => r.strike)).toEqual([1, 4, 3, 2]);
    expect(sortRows(varied, { key: "call:oi", dir: "asc" }).map((r) => r.strike)).toEqual([3, 4, 1, 2]);
  });
  it("puts rows with no value last in both directions", () => {
    expect(sortRows(varied, { key: "call:oi", dir: "desc" }).at(-1)!.strike).toBe(2);
    expect(sortRows(varied, { key: "call:oi", dir: "asc" }).at(-1)!.strike).toBe(2);
  });
  it("ties fall back to strike order so rows never jitter", () => {
    const tied = [row(3), row(1), row(2)];
    expect(sortRows(tied, { key: "put:iv", dir: "desc" }).map((r) => r.strike)).toEqual([1, 2, 3]);
  });
  it("does not mutate its input", () => {
    const copy = [...varied];
    sortRows(varied, { key: "call:oi", dir: "desc" });
    expect(varied).toEqual(copy);
  });
  it("header clicks cycle desc, asc, then back to strike order", () => {
    let s = nextSort(DEFAULT_SORT, "call:oi");
    expect(s).toEqual({ key: "call:oi", dir: "desc" });
    s = nextSort(s, "call:oi");
    expect(s).toEqual({ key: "call:oi", dir: "asc" });
    expect(nextSort(s, "call:oi")).toEqual(DEFAULT_SORT);
    expect(nextSort(DEFAULT_SORT, "strike")).toEqual({ key: "strike", dir: "desc" });
    expect(nextSort({ key: "strike", dir: "desc" }, "strike")).toEqual({ key: "strike", dir: "asc" });
  });
});
