/**
 * Option chain presentation rules: which columns exist, what counts as in or out of the money, how rows are
 * filtered and sorted. Pure functions so the behaviour is testable without rendering a table.
 */
import { fmt, fmtCompact, fmtInt } from "./format";
import type { StrikeRow } from "../api/types";

export type Side = "call" | "put";
export type ColumnKey = "ltp" | "bid" | "ask" | "delta" | "iv" | "gamma" | "theta" | "vega" | "volume" | "oi" | "oichg";

export interface ColumnDef {
  key: ColumnKey;
  label: string;
  /** Full name for screen readers and tooltips. */
  title: string;
  format: (value: number | null | undefined) => string;
}

const signedCompact = (v: number | null | undefined): string => (v === null || v === undefined ? "—" : `${v > 0 ? "+" : v < 0 ? "−" : ""}${fmtCompact(Math.abs(v))}`);

/** Ordered from the strike outwards: calls render this list reversed so LTP always sits beside the strike. */
export const COLUMNS: readonly ColumnDef[] = [
  { key: "ltp", label: "LTP", title: "Last traded price", format: (v) => fmt(v) },
  { key: "bid", label: "Bid", title: "Best bid", format: (v) => fmt(v) },
  { key: "ask", label: "Ask", title: "Best ask", format: (v) => fmt(v) },
  { key: "delta", label: "Δ", title: "Delta", format: (v) => fmt(v, 3) },
  { key: "iv", label: "IV", title: "Implied volatility, percent", format: (v) => (v === null || v === undefined ? "—" : `${fmt(v, 1)}%`) },
  { key: "gamma", label: "Γ", title: "Gamma", format: (v) => fmt(v, 5) },
  { key: "theta", label: "Θ", title: "Theta per day", format: (v) => fmt(v, 2) },
  { key: "vega", label: "Vega", title: "Vega per volatility point", format: (v) => fmt(v, 2) },
  { key: "volume", label: "Vol", title: "Volume", format: (v) => fmtCompact(v) },
  { key: "oi", label: "OI", title: "Open interest", format: (v) => fmtCompact(v) },
  { key: "oichg", label: "OI chg", title: "Open interest change since the previous close", format: signedCompact },
];

export const DEFAULT_COLUMNS: readonly ColumnKey[] = ["ltp", "delta", "iv", "oi"];

const FIELD: Record<ColumnKey, string> = {
  ltp: "ltp", bid: "bid", ask: "ask", delta: "delta", iv: "iv", gamma: "gamma", theta: "theta", vega: "vega", volume: "volume", oi: "oi", oichg: "oi_change",
};

export function cellValue(row: StrikeRow, side: Side, col: ColumnKey): number | null {
  const v = (row as unknown as Record<string, number | null | undefined>)[`${side}_${FIELD[col]}`];
  return v ?? null;
}

/** The visible columns in render order for one side: puts read outward from the strike, calls are mirrored. */
export function orderedColumns(visible: Iterable<ColumnKey>, side: Side): ColumnDef[] {
  const set = new Set(visible);
  const cols = COLUMNS.filter((c) => set.has(c.key));
  return side === "call" ? [...cols].reverse() : cols;
}

export type Moneyness = "ITM" | "ATM" | "OTM";

/** A call is in the money below the underlying, a put above it. The ATM row is the engine's chosen strike. */
export function moneyness(strike: number, spot: number | null, side: Side, isAtm: boolean): Moneyness {
  if (isAtm) return "ATM";
  if (spot === null) return "OTM";
  return side === "call" ? (strike < spot ? "ITM" : "OTM") : strike > spot ? "ITM" : "OTM";
}

export type Window = number | "all";

export interface Filter {
  window: Window;
  atm: number | null;
  /** Digits typed by the user: keeps strikes whose number contains them. */
  query: string;
}

export function filterRows(rows: readonly StrikeRow[], f: Filter): StrikeRow[] {
  let out = [...rows].sort((a, b) => a.strike - b.strike);
  const q = f.query.replace(/\D/g, "");
  if (q) return out.filter((r) => String(Math.trunc(r.strike)).includes(q));
  if (f.window !== "all") {
    const at = out.findIndex((r) => r.is_atm || r.strike === f.atm);
    if (at >= 0) out = out.slice(Math.max(0, at - f.window), at + f.window + 1);
  }
  return out;
}

export interface Sort {
  /** "strike" or "<side>:<column>" */
  key: string;
  dir: "asc" | "desc";
}
export const DEFAULT_SORT: Sort = { key: "strike", dir: "asc" };

/** Rows with no value for the sort column go last, whichever direction is chosen. */
export function sortRows(rows: readonly StrikeRow[], sort: Sort): StrikeRow[] {
  const sign = sort.dir === "asc" ? 1 : -1;
  const value = (r: StrikeRow): number | null => {
    if (sort.key === "strike") return r.strike;
    const [side, col] = sort.key.split(":") as [Side, ColumnKey];
    return cellValue(r, side, col);
  };
  return [...rows].sort((a, b) => {
    const x = value(a), y = value(b);
    if (x === null && y === null) return a.strike - b.strike;
    if (x === null) return 1;
    if (y === null) return -1;
    return x === y ? a.strike - b.strike : (x - y) * sign;
  });
}

/** Header click: unsorted -> ascending -> descending -> back to the natural strike order. */
export function nextSort(current: Sort, key: string): Sort {
  if (current.key !== key) return { key, dir: key === "strike" ? "asc" : "desc" };
  if (current.dir === "desc" && key !== "strike") return { key, dir: "asc" };
  return key === "strike" ? { key, dir: current.dir === "asc" ? "desc" : "asc" } : DEFAULT_SORT;
}

export const strikeLabel = (v: number): string => fmtInt(v);
