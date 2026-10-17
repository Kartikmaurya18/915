/**
 * Options strategies: what a set of legs pays at expiry, where it breaks even, and how it is marked today.
 *
 * Three numbers are deliberately kept apart because they answer different questions:
 *   - expiry payoff   exact and model-free: intrinsic value at settlement minus the premium paid/received
 *   - today (model)   a Black-Scholes estimate at each leg's current implied volatility; an approximation
 *   - mark-to-market  live: each leg's current traded price against the price it was added at
 * Pure functions, no React: every rule is testable without a browser.
 */
import { bsPrice, type OptionType } from "./bs";
import type { StrikeRow } from "../api/types";

export type { OptionType };
export type Side = "BUY" | "SELL";

export interface StrategyLeg {
  id: string;
  strike: number;
  optionType: OptionType;
  side: Side;
  /** Units, as in the paper-trading API. */
  quantity: number;
  /** Price per unit when the leg was added (the live LTP at that moment). */
  premium: number;
}

const sign = (side: Side): 1 | -1 => (side === "BUY" ? 1 : -1);
const intrinsic = (type: OptionType, strike: number, spot: number): number => (type === "CE" ? Math.max(spot - strike, 0) : Math.max(strike - spot, 0));

/** One leg's profit or loss if the underlying settles at `spot`. */
export const legPayoff = (leg: StrategyLeg, spot: number): number => sign(leg.side) * (intrinsic(leg.optionType, leg.strike, spot) - leg.premium) * leg.quantity;

export const payoffAt = (legs: readonly StrategyLeg[], spot: number): number => legs.reduce((sum, leg) => sum + legPayoff(leg, spot), 0);

/** Positive = credit received, negative = debit paid. */
export const netPremium = (legs: readonly StrategyLeg[]): number => legs.reduce((sum, l) => sum - sign(l.side) * l.premium * l.quantity, 0);

export interface StrategyMetrics {
  netPremium: number;
  kind: "debit" | "credit" | "flat";
  /** null = unlimited */
  maxProfit: number | null;
  maxLoss: number | null;
  breakevens: number[];
  /** Max profit / max loss when both are finite. */
  riskReward: number | null;
}

const round2 = (n: number): number => Math.round(n * 100) / 100;

/**
 * Payoff is piecewise linear with kinks only at the strikes, so every figure below is exact:
 * evaluate at 0 and at each strike, and read the slope beyond the highest strike for the open end.
 */
export function metrics(legs: readonly StrategyLeg[]): StrategyMetrics | null {
  if (legs.length === 0) return null;
  const kinks = [...new Set(legs.map((l) => l.strike))].sort((a, b) => a - b);
  const xs = [0, ...kinks];
  const ys = xs.map((x) => payoffAt(legs, x));
  const top = kinks[kinks.length - 1]!;
  const tailSlope = payoffAt(legs, top + 1) - payoffAt(legs, top);

  const breakevens: number[] = [];
  for (let i = 0; i < xs.length; i++) {
    if (Math.abs(ys[i]!) < 1e-9 && xs[i]! > 0) breakevens.push(xs[i]!);
    if (i > 0 && ys[i - 1]! * ys[i]! < 0) {
      const x0 = xs[i - 1]!, x1 = xs[i]!, y0 = ys[i - 1]!, y1 = ys[i]!;
      breakevens.push(x0 + ((0 - y0) * (x1 - x0)) / (y1 - y0));
    }
  }
  if (Math.abs(tailSlope) > 1e-9) {
    const root = top - ys[ys.length - 1]! / tailSlope;
    if (root > top) breakevens.push(root);
  }

  const best = Math.max(...ys), worst = Math.min(...ys);
  const maxProfit = tailSlope > 1e-9 ? null : round2(best);
  const maxLoss = tailSlope < -1e-9 ? null : round2(worst);
  const net = round2(netPremium(legs));
  return {
    netPremium: net,
    kind: net > 0 ? "credit" : net < 0 ? "debit" : "flat",
    maxProfit,
    maxLoss,
    breakevens: [...new Set(breakevens.map(round2))].sort((a, b) => a - b),
    riskReward: maxProfit !== null && maxLoss !== null && maxLoss < 0 ? round2(maxProfit / -maxLoss) : null,
  };
}

/** Evenly spaced underlying levels spanning every strike with padding, plus the strikes themselves (the kinks). */
export function spotGrid(legs: readonly StrategyLeg[], spot: number, points = 160): number[] {
  const strikes = legs.map((l) => l.strike);
  const lo = Math.min(spot, ...strikes), hi = Math.max(spot, ...strikes);
  const pad = Math.max((hi - lo) * 0.25, spot * 0.02);
  const start = Math.max(0, lo - pad), end = hi + pad;
  const grid = Array.from({ length: points }, (_, i) => start + ((end - start) * i) / (points - 1));
  return [...new Set([...grid, ...strikes])].sort((a, b) => a - b);
}

const rowFor = (rows: readonly StrikeRow[], strike: number): StrikeRow | undefined => rows.find((r) => r.strike === strike);
export const ltpOf = (row: StrikeRow | undefined, type: OptionType): number | null => (row ? (type === "CE" ? row.call_ltp : row.put_ltp) : null);
const ivOf = (row: StrikeRow | undefined, type: OptionType): number | null => (row ? (type === "CE" ? row.call_iv : row.put_iv) : null);

/** Live P&L of the legs against the chain now; null if any leg has no current price. */
export function markToMarket(legs: readonly StrategyLeg[], rows: readonly StrikeRow[]): number | null {
  let total = 0;
  for (const leg of legs) {
    const now = ltpOf(rowFor(rows, leg.strike), leg.optionType);
    if (now === null) return null;
    total += sign(leg.side) * (now - leg.premium) * leg.quantity;
  }
  return total;
}

/** The strategy's value across underlying levels today, at each leg's current IV. Null when IV or time is unknown. */
export function modelCurve(legs: readonly StrategyLeg[], rows: readonly StrikeRow[], years: number | null, spots: readonly number[]): number[] | null {
  if (years === null || years <= 0) return null;
  const vols: number[] = [];
  for (const leg of legs) {
    const iv = ivOf(rowFor(rows, leg.strike), leg.optionType);
    if (iv === null || iv <= 0) return null;
    vols.push(iv / 100);
  }
  return spots.map((s) => legs.reduce((sum, leg, i) => sum + sign(leg.side) * (bsPrice(leg.optionType, s, leg.strike, years, vols[i]!) - leg.premium) * leg.quantity, 0));
}

// -- presets ---------------------------------------------------------------------------------

export interface TemplateLeg {
  /** Strikes away from the at-the-money strike along the chain (negative = lower). */
  offset: number;
  optionType: OptionType;
  side: Side;
}
export interface StrategyTemplate {
  id: string;
  label: string;
  view: string;
  legs: TemplateLeg[];
}

const leg = (offset: number, optionType: OptionType, side: Side): TemplateLeg => ({ offset, optionType, side });

export const TEMPLATES: readonly StrategyTemplate[] = [
  { id: "long-call", label: "Long call", view: "Bullish; risk limited to the premium", legs: [leg(0, "CE", "BUY")] },
  { id: "long-put", label: "Long put", view: "Bearish; risk limited to the premium", legs: [leg(0, "PE", "BUY")] },
  { id: "short-call", label: "Short call", view: "Neutral to bearish; risk is unlimited", legs: [leg(0, "CE", "SELL")] },
  { id: "short-put", label: "Short put", view: "Neutral to bullish; large downside risk", legs: [leg(0, "PE", "SELL")] },
  { id: "long-straddle", label: "Long straddle", view: "Expects a big move either way", legs: [leg(0, "CE", "BUY"), leg(0, "PE", "BUY")] },
  { id: "short-straddle", label: "Short straddle", view: "Expects little movement; risk is unlimited", legs: [leg(0, "CE", "SELL"), leg(0, "PE", "SELL")] },
  { id: "long-strangle", label: "Long strangle", view: "Cheaper bet on a big move", legs: [leg(2, "CE", "BUY"), leg(-2, "PE", "BUY")] },
  { id: "short-strangle", label: "Short strangle", view: "Expects a range; risk is unlimited", legs: [leg(2, "CE", "SELL"), leg(-2, "PE", "SELL")] },
  { id: "bull-call-spread", label: "Bull call spread", view: "Moderately bullish; capped both ways", legs: [leg(0, "CE", "BUY"), leg(2, "CE", "SELL")] },
  { id: "bear-put-spread", label: "Bear put spread", view: "Moderately bearish; capped both ways", legs: [leg(0, "PE", "BUY"), leg(-2, "PE", "SELL")] },
  { id: "iron-condor", label: "Iron condor", view: "Expects a range; risk capped by the wings", legs: [leg(-4, "PE", "BUY"), leg(-2, "PE", "SELL"), leg(2, "CE", "SELL"), leg(4, "CE", "BUY")] },
];

let counter = 0;
export const newLegId = (): string => `leg-${Date.now().toString(36)}-${(counter++).toString(36)}`;

export interface BuildResult {
  legs: StrategyLeg[];
  /** Why the preset could not be built from this chain, if it could not. */
  problem: string | null;
}

/** Instantiate a preset on the live chain: strikes picked by offset from ATM, premiums read from the chain. */
export function buildFromTemplate(template: StrategyTemplate, rows: readonly StrikeRow[], atmStrike: number | null, quantity: number): BuildResult {
  const sorted = [...rows].sort((a, b) => a.strike - b.strike);
  const atIndex = sorted.findIndex((r) => r.strike === atmStrike);
  if (sorted.length === 0 || atIndex < 0) return { legs: [], problem: "The option chain has no at-the-money strike yet." };
  const legs: StrategyLeg[] = [];
  for (const t of template.legs) {
    const row = sorted[atIndex + t.offset];
    if (!row) return { legs: [], problem: `${template.label} needs strikes further from the money than this chain offers.` };
    const premium = ltpOf(row, t.optionType);
    if (premium === null || premium <= 0) return { legs: [], problem: `No price for the ${row.strike} ${t.optionType} yet.` };
    legs.push({ id: newLegId(), strike: row.strike, optionType: t.optionType, side: t.side, quantity, premium });
  }
  return { legs, problem: null };
}

/** Re-read a leg's premium from the chain after the user changes its strike or type. */
export function repriceLeg(l: StrategyLeg, rows: readonly StrikeRow[]): StrategyLeg | null {
  const premium = ltpOf(rowFor(rows, l.strike), l.optionType);
  return premium === null || premium <= 0 ? null : { ...l, premium };
}

/** Strategies with no loss limit on at least one side, so the UI can say so plainly. */
export const hasUnlimitedRisk = (m: StrategyMetrics | null): boolean => m !== null && m.maxLoss === null;
