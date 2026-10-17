import { describe, expect, it } from "vitest";
import { aggregateDaily, dayToTime, inRange, type StoredCandle } from "../src/lib/daily";

const c = (open_time: string, open: number, high: number, low: number, close: number): StoredCandle => ({ open_time, open, high, low, close });

describe("aggregateDaily", () => {
  it("builds one bar per day: first open, last close, highest high, lowest low", () => {
    const bars = aggregateDaily([
      c("2026-10-15T09:15:00+05:30", 100, 105, 99, 103),
      c("2026-10-15T09:30:00+05:30", 103, 110, 102, 108),
      c("2026-10-15T15:15:00+05:30", 108, 109, 95, 96),
      c("2026-10-16T09:15:00+05:30", 97, 99, 96, 98),
    ]);
    expect(bars).toHaveLength(2);
    expect(bars[0]).toEqual({ time: dayToTime("2026-10-15"), open: 100, high: 110, low: 95, close: 96 });
    expect(bars[1]).toEqual({ time: dayToTime("2026-10-16"), open: 97, high: 99, low: 96, close: 98 });
  });

  it("does not depend on the order of the input", () => {
    const rows = [c("2026-10-15T09:30:00+05:30", 2, 3, 1, 2.5), c("2026-10-15T09:15:00+05:30", 1, 2, 0.5, 1.5)];
    expect(aggregateDaily(rows)).toEqual(aggregateDaily([...rows].reverse()));
    expect(aggregateDaily(rows)[0]).toMatchObject({ open: 1, close: 2.5 });
  });

  it("uses the stored IST date, so a late-evening candle stays on its own day", () => {
    const bars = aggregateDaily([c("2026-10-15T23:55:00+05:30", 1, 1, 1, 1), c("2026-10-16T00:05:00+05:30", 2, 2, 2, 2)]);
    expect(bars.map((b) => b.time)).toEqual([dayToTime("2026-10-15"), dayToTime("2026-10-16")]);
  });

  it("returns days in ascending order and ignores rows without a valid date", () => {
    const bars = aggregateDaily([c("2026-10-16T09:15:00+05:30", 1, 1, 1, 1), c("garbage", 9, 9, 9, 9), c("2026-10-14T09:15:00+05:30", 1, 1, 1, 1)]);
    expect(bars.map((b) => b.time)).toEqual([dayToTime("2026-10-14"), dayToTime("2026-10-16")]);
    expect(aggregateDaily([])).toEqual([]);
  });
});

describe("inRange", () => {
  const day = (iso: string) => ({ time: dayToTime(iso), open: 1, high: 1, low: 1, close: 1 });
  const bars = [day("2025-09-01"), day("2026-08-01"), day("2026-09-20"), day("2026-10-09"), day("2026-10-12"), day("2026-10-16")];

  it("counts back from the most recent bar", () => {
    expect(inRange(bars, "1W").map((b) => b.time)).toEqual([dayToTime("2026-10-12"), dayToTime("2026-10-16")]); // 10-09 is exactly 7 days back: excluded
    expect(inRange(bars, "1M").map((b) => b.time)).toEqual([dayToTime("2026-09-20"), dayToTime("2026-10-09"), dayToTime("2026-10-12"), dayToTime("2026-10-16")]);
    expect(inRange(bars, "1Y")).toHaveLength(5); // the 2025-09-01 bar is more than a year before the last
  });

  it("ALL and 1D return everything given, and an empty series stays empty", () => {
    expect(inRange(bars, "ALL")).toHaveLength(6);
    expect(inRange(bars, "1D")).toHaveLength(6);
    expect(inRange([], "1M")).toEqual([]);
  });
});
