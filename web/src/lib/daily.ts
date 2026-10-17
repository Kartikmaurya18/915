import type { Bar } from "../api/types";

/** A candle as stored in history (`/api/history/candles`): ISO times with the +05:30 offset, no epoch field. */
export interface StoredCandle {
  open_time: string;
  open: number;
  high: number;
  low: number;
  close: number;
}

export type Range = "1D" | "1W" | "1M" | "1Y" | "ALL";
export const RANGES: readonly Range[] = ["1D", "1W", "1M", "1Y", "ALL"];

/** Calendar days each range reaches back from the last bar. ALL keeps everything. */
const DAYS: Record<Exclude<Range, "ALL" | "1D">, number> = { "1W": 7, "1M": 30, "1Y": 365 };

const DAY_SECONDS = 86_400;

/** "2026-10-15" -> epoch seconds of that date at 00:00 UTC. The chart prints dates in IST, which is the same calendar day. */
export function dayToTime(isoDate: string): number {
  const [y, m, d] = isoDate.split("-").map(Number);
  return Date.UTC(y ?? 1970, (m ?? 1) - 1, d ?? 1) / 1000;
}

/**
 * Collapse intraday candles into one bar per Asia/Kolkata trading day: open of the first, close of the last,
 * highest high, lowest low. The date comes from the stored ISO time itself (it already carries the IST offset),
 * so no time-zone arithmetic is involved. Input order does not matter; output is ascending by day.
 */
export function aggregateDaily(candles: readonly StoredCandle[]): Bar[] {
  const days = new Map<string, { first: StoredCandle; last: StoredCandle; high: number; low: number }>();
  for (const c of candles) {
    const date = c.open_time.slice(0, 10);
    if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) continue;
    const d = days.get(date);
    if (!d) {
      days.set(date, { first: c, last: c, high: c.high, low: c.low });
    } else {
      if (c.open_time < d.first.open_time) d.first = c;
      if (c.open_time > d.last.open_time) d.last = c;
      d.high = Math.max(d.high, c.high);
      d.low = Math.min(d.low, c.low);
    }
  }
  return [...days.entries()]
    .sort(([a], [b]) => (a < b ? -1 : 1))
    .map(([date, d]) => ({ time: dayToTime(date), open: d.first.open, high: d.high, low: d.low, close: d.last.close }));
}

/** The bars inside a range, counted back from the most recent bar (not from today, so a quiet week still shows data). */
export function inRange(bars: readonly Bar[], range: Range): Bar[] {
  if (range === "ALL" || range === "1D" || bars.length === 0) return [...bars];
  const cutoff = bars[bars.length - 1]!.time - DAYS[range] * DAY_SECONDS;
  return bars.filter((b) => b.time > cutoff);
}
