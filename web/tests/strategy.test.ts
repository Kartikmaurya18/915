import { describe, expect, it } from "vitest";
import { bsPrice, expiryInstant, yearsToExpiry } from "../src/lib/bs";
import { TEMPLATES, buildFromTemplate, markToMarket, metrics, modelCurve, netPremium, payoffAt, repriceLeg, spotGrid, type StrategyLeg } from "../src/lib/strategy";
import type { StrikeRow } from "../src/api/types";

const leg = (strike: number, optionType: "CE" | "PE", side: "BUY" | "SELL", premium: number, quantity = 1): StrategyLeg => ({ id: `${strike}${optionType}${side}`, strike, optionType, side, quantity, premium });

describe("Black-Scholes in the browser", () => {
  it("matches the Python reference (src/option_chain/analytics.py)", () => {
    const t = 7 / 365;
    expect(bsPrice("CE", 24850, 24900, t, 0.14)).toBeCloseTo(180.15782, 3);
    expect(bsPrice("PE", 24850, 24900, t, 0.14)).toBeCloseTo(204.85568, 3);
    expect(bsPrice("CE", 100, 90, 0.01, 0.3)).toBeCloseTo(10.04663, 4);
  });
  it("falls back to intrinsic value with no time or volatility", () => {
    expect(bsPrice("CE", 110, 100, 0, 0.2)).toBe(10);
    expect(bsPrice("PE", 110, 100, 0.1, 0)).toBe(0);
  });
  it("parses both expiry formats as 15:30 IST", () => {
    expect(expiryInstant("28-Oct-2026")).toBe(Date.UTC(2026, 9, 28, 10, 0));
    expect(expiryInstant("2026-10-28")).toBe(Date.UTC(2026, 9, 28, 10, 0));
    expect(expiryInstant("nonsense")).toBeNull();
    expect(expiryInstant(null)).toBeNull();
  });
  it("gives years to settlement, or null once past", () => {
    const now = Date.UTC(2026, 9, 27, 10, 0);
    expect(yearsToExpiry("28-Oct-2026", now)).toBeCloseTo(1 / 365, 9);
    expect(yearsToExpiry("28-Oct-2026", Date.UTC(2026, 9, 28, 11, 0))).toBeNull();
  });
});

describe("expiry payoff, worked by hand", () => {
  it("long call: loses the premium, breaks even at strike + premium, unlimited profit", () => {
    const legs = [leg(100, "CE", "BUY", 5, 10)];
    expect(payoffAt(legs, 80)).toBe(-50);
    expect(payoffAt(legs, 120)).toBe(150);
    expect(metrics(legs)).toMatchObject({ netPremium: -50, kind: "debit", maxProfit: null, maxLoss: -50, breakevens: [105], riskReward: null });
  });
  it("short put: keeps the premium, loses a lot if the index collapses (finite: it cannot go below zero)", () => {
    const m = metrics([leg(100, "PE", "SELL", 4)])!;
    expect(m.maxProfit).toBe(4);
    expect(m.maxLoss).toBe(-96); // 4 collected - 100 strike, settling at 0
    expect(m.breakevens).toEqual([96]);
    expect(m.kind).toBe("credit");
  });
  it("short call has unlimited loss", () => {
    const m = metrics([leg(100, "CE", "SELL", 5)])!;
    expect(m.maxLoss).toBeNull();
    expect(m.maxProfit).toBe(5);
  });
  it("long straddle: two breakevens, capped loss, unlimited profit", () => {
    const m = metrics([leg(100, "CE", "BUY", 6), leg(100, "PE", "BUY", 4)])!;
    expect(m.netPremium).toBe(-10);
    expect(m.breakevens).toEqual([90, 110]);
    expect(m).toMatchObject({ maxLoss: -10, maxProfit: null });
  });
  it("short strangle: unlimited loss both ways", () => {
    const m = metrics([leg(110, "CE", "SELL", 3), leg(90, "PE", "SELL", 2)])!;
    expect(m.breakevens).toEqual([85, 115]);
    expect(m.maxProfit).toBe(5);
    expect(m.maxLoss).toBeNull();
  });
  it("bull call spread: both sides capped", () => {
    const m = metrics([leg(100, "CE", "BUY", 8), leg(110, "CE", "SELL", 3)])!;
    expect(m).toMatchObject({ netPremium: -5, maxLoss: -5, maxProfit: 5, breakevens: [105], riskReward: 1 });
  });
  it("bear put spread", () => {
    const m = metrics([leg(100, "PE", "BUY", 7), leg(90, "PE", "SELL", 3)])!;
    expect(m).toMatchObject({ netPremium: -4, maxLoss: -4, maxProfit: 6, breakevens: [96], riskReward: 1.5 });
  });
  it("iron condor: capped at the wings, two breakevens", () => {
    const legs = [leg(80, "PE", "BUY", 1), leg(90, "PE", "SELL", 3), leg(110, "CE", "SELL", 3), leg(120, "CE", "BUY", 1)];
    const m = metrics(legs)!;
    expect(m).toMatchObject({ netPremium: 4, kind: "credit", maxProfit: 4, maxLoss: -6, breakevens: [86, 114] });
    expect(payoffAt(legs, 100)).toBe(4);
    expect(payoffAt(legs, 70)).toBe(-6);
  });
  it("scales with quantity and sums several legs of one strike", () => {
    const legs = [leg(100, "CE", "BUY", 5, 50)];
    expect(netPremium(legs)).toBe(-250);
    expect(payoffAt(legs, 130)).toBe(1250);
  });
  it("has no metrics without legs", () => {
    expect(metrics([])).toBeNull();
  });
});

describe("spot grid", () => {
  it("covers every strike plus padding and never goes below zero", () => {
    const g = spotGrid([leg(24800, "CE", "BUY", 1), leg(25200, "PE", "SELL", 1)], 25000);
    expect(g[0]!).toBeLessThan(24800);
    expect(g[g.length - 1]!).toBeGreaterThan(25200);
    expect(g).toContain(24800);
    expect(g.every((x) => x >= 0)).toBe(true);
    expect([...g].sort((a, b) => a - b)).toEqual(g);
  });
});

const row = (strike: number, ce: number | null, pe: number | null, iv: number | null = 14): StrikeRow =>
  ({ strike, call_ltp: ce, put_ltp: pe, call_iv: iv, put_iv: iv, is_atm: strike === 100 }) as StrikeRow;
const chain = [90, 95, 100, 105, 110].map((k) => row(k, Math.max(100 - k, 0) + 3, Math.max(k - 100, 0) + 3));

describe("live mark-to-market and the model curve", () => {
  it("marks each leg against its added price", () => {
    const legs = [leg(100, "CE", "BUY", 2, 10), leg(105, "PE", "SELL", 7, 10)];
    // CE ltp now 3 (+1 * 10 long), PE ltp now 8 (short, price up 1 -> -10)
    expect(markToMarket(legs, chain)).toBe(0);
  });
  it("is unknown rather than wrong when a leg has no price", () => {
    expect(markToMarket([leg(100, "CE", "BUY", 2)], [row(100, null, null)])).toBeNull();
    expect(markToMarket([leg(999, "CE", "BUY", 2)], chain)).toBeNull();
  });
  it("model curve converges to the expiry payoff as time runs out", () => {
    const legs = [leg(100, "CE", "BUY", 3)];
    const spots = [90, 100, 110];
    const near = modelCurve(legs, chain, 1e-7, spots)!;
    spots.forEach((s, i) => expect(near[i]).toBeCloseTo(payoffAt(legs, s), 2));
    const far = modelCurve(legs, chain, 0.5, spots)!;
    expect(far[0]!).toBeGreaterThan(payoffAt(legs, 90)); // time value is still worth something out of the money
  });
  it("model curve is withheld without IV or time", () => {
    const legs = [leg(100, "CE", "BUY", 3)];
    expect(modelCurve(legs, [row(100, 3, 3, null)], 0.1, [100])).toBeNull();
    expect(modelCurve(legs, chain, null, [100])).toBeNull();
    expect(modelCurve(legs, chain, 0, [100])).toBeNull();
  });
});

describe("presets on a chain", () => {
  const wide = Array.from({ length: 11 }, (_, i) => row(24600 + i * 50, 300 - i * 25, 25 + i * 25));
  const atm = 24850;
  it("builds every preset with strikes picked by offset from the money", () => {
    for (const t of TEMPLATES) {
      const r = buildFromTemplate(t, wide, atm, 50);
      expect(r.problem, t.id).toBeNull();
      expect(r.legs).toHaveLength(t.legs.length);
      expect(r.legs.every((l) => l.quantity === 50 && l.premium > 0)).toBe(true);
    }
  });
  it("iron condor is two buys and two sells", () => {
    const legs = buildFromTemplate(TEMPLATES.find((t) => t.id === "iron-condor")!, wide, atm, 50).legs;
    expect(legs.map((l) => `${l.side}${l.strike}${l.optionType}`)).toEqual(["BUY24650PE", "SELL24750PE", "SELL24950CE", "BUY25050CE"]);
  });
  it("explains why a preset cannot be built instead of guessing", () => {
    const tiny = wide.slice(4, 7);
    expect(buildFromTemplate(TEMPLATES.find((t) => t.id === "iron-condor")!, tiny, atm, 50).problem).toMatch(/further from the money/);
    expect(buildFromTemplate(TEMPLATES[0]!, [], null, 50).problem).toMatch(/no at-the-money/);
    expect(buildFromTemplate(TEMPLATES[0]!, [row(24850, null, 1)].map((r) => ({ ...r, is_atm: true })), atm, 50).problem).toMatch(/No price/);
  });
  it("repricing follows the chain and refuses an unpriced strike", () => {
    const l = leg(24850, "CE", "BUY", 1);
    expect(repriceLeg(l, wide)!.premium).toBe(wide.find((r) => r.strike === 24850)!.call_ltp);
    expect(repriceLeg({ ...l, strike: 1 }, wide)).toBeNull();
  });
});
