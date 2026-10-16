import { useEffect, useRef } from "react";
import { createChart, type IChartApi, type ISeriesApi, type UTCTimestamp } from "lightweight-charts";
import type { Bar } from "../api/types";

function cssVar(name: string, fallback: string): string {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return v || fallback;
}

// The chart library formats times in UTC; market time is IST, so supply the formatters.
const IST = "Asia/Kolkata";
const clock = new Intl.DateTimeFormat("en-IN", { timeZone: IST, hour: "2-digit", minute: "2-digit", hour12: false });
const day = new Intl.DateTimeFormat("en-IN", { timeZone: IST, day: "2-digit", month: "short" });
const full = new Intl.DateTimeFormat("en-IN", { timeZone: IST, day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", hour12: false });
const asDate = (t: unknown) => new Date((typeof t === "number" ? t : 0) * 1000);

function themeOptions() {
  return {
    layout: {
      background: { color: cssVar("--panel", "#ffffff") },
      textColor: cssVar("--muted", "#5b6572"),
    },
    grid: {
      vertLines: { color: cssVar("--line", "#e3e7ec") },
      horzLines: { color: cssVar("--line", "#e3e7ec") },
    },
    rightPriceScale: { borderColor: cssVar("--line", "#e3e7ec") },
    localization: { timeFormatter: (t: unknown) => full.format(asDate(t)) },
    timeScale: {
      borderColor: cssVar("--line", "#e3e7ec"),
      timeVisible: true,
      secondsVisible: false,
      // 0 = year, 1 = month, 2 = day of month, 3 = time, 4 = time with seconds
      tickMarkFormatter: (t: unknown, type: number) => (type <= 2 ? day.format(asDate(t)) : clock.format(asDate(t))),
    },
  };
}

const toBar = (c: Bar) => ({
  time: c.time as UTCTimestamp,
  open: c.open,
  high: c.high,
  low: c.low,
  close: c.close,
});

/**
 * Candlestick chart. `series` is the full ordered list; the common tick only changes the last bar,
 * so that case uses `update()` and everything else (first load, new interval) uses `setData()`.
 */
export function CandleChart({ series, resetKey }: { series: Bar[]; resetKey: string }) {
  const host = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi | null>(null);
  const bars = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const shown = useRef<{ key: string; length: number; first: number } | null>(null);

  useEffect(() => {
    if (!host.current) return;
    const api = createChart(host.current, { autoSize: true, ...themeOptions() });
    const candles = api.addCandlestickSeries({
      upColor: cssVar("--pos", "#16a34a"),
      downColor: cssVar("--neg", "#dc2626"),
      borderVisible: false,
      wickUpColor: cssVar("--pos", "#16a34a"),
      wickDownColor: cssVar("--neg", "#dc2626"),
    });
    chart.current = api;
    bars.current = candles;
    shown.current = null;

    const media = window.matchMedia("(prefers-color-scheme: dark)");
    const onTheme = () => api.applyOptions(themeOptions());
    media.addEventListener("change", onTheme);
    return () => {
      media.removeEventListener("change", onTheme);
      api.remove();
      chart.current = null;
      bars.current = null;
    };
  }, []);

  useEffect(() => {
    const target = bars.current;
    if (!target || series.length === 0) return;
    const last = series[series.length - 1]!;
    const prev = shown.current;
    const incremental =
      prev !== null && prev.key === resetKey && prev.first === series[0]!.time && (series.length === prev.length || series.length === prev.length + 1);
    if (incremental) {
      target.update(toBar(last));
    } else {
      target.setData(series.map(toBar));
      chart.current?.timeScale().fitContent();
    }
    shown.current = { key: resetKey, length: series.length, first: series[0]!.time };
  }, [series, resetKey]);

  return <div ref={host} className="chart" role="img" aria-label="Candlestick chart" />;
}
