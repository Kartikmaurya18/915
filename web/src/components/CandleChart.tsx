import { useEffect, useRef } from "react";
import { createChart, type IChartApi, type ISeriesApi, type UTCTimestamp } from "lightweight-charts";
import type { Bar } from "../api/types";

function token(name: string, fallback: string): string {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return v || fallback;
}

// The chart library formats times in UTC; market time is IST, so supply the formatters.
const IST = "Asia/Kolkata";
const clock = new Intl.DateTimeFormat("en-IN", { timeZone: IST, hour: "2-digit", minute: "2-digit", hour12: false });
const day = new Intl.DateTimeFormat("en-IN", { timeZone: IST, day: "2-digit", month: "short" });
const full = new Intl.DateTimeFormat("en-IN", { timeZone: IST, day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", hour12: false });
const fullDay = new Intl.DateTimeFormat("en-IN", { timeZone: IST, day: "2-digit", month: "short", year: "numeric" });
const asDate = (t: unknown) => new Date((typeof t === "number" ? t : 0) * 1000);

function themeOptions(daily: boolean) {
  const line = token("--line", "#e2e8f0");
  return {
    layout: { background: { color: token("--surface", "#ffffff") }, textColor: token("--muted", "#475569"), fontFamily: "Inter Variable, system-ui, sans-serif" },
    grid: { vertLines: { color: line }, horzLines: { color: line } },
    rightPriceScale: { borderColor: line },
    crosshair: {
      vertLine: { color: token("--subtle", "#5a687c"), labelBackgroundColor: token("--brand", "#0a7f61") },
      horzLine: { color: token("--subtle", "#5a687c"), labelBackgroundColor: token("--brand", "#0a7f61") },
    },
    localization: { timeFormatter: (t: unknown) => (daily ? fullDay : full).format(asDate(t)) },
    timeScale: {
      borderColor: line,
      timeVisible: !daily,
      secondsVisible: false,
      // 0 = year, 1 = month, 2 = day of month, 3 = time, 4 = time with seconds
      tickMarkFormatter: (t: unknown, type: number) => (type <= 2 || daily ? day.format(asDate(t)) : clock.format(asDate(t))),
    },
  };
}

const toBar = (c: Bar) => ({ time: c.time as UTCTimestamp, open: c.open, high: c.high, low: c.low, close: c.close });
const toPoint = (c: Bar) => ({ time: c.time as UTCTimestamp, value: c.close });

interface Props {
  series: Bar[];
  /** Changes when the data set changes identity (another interval or strike), forcing a full reload instead of an update. */
  resetKey: string;
  mode?: "candles" | "line";
  /** Daily bars: show dates rather than clock times. */
  daily?: boolean;
  /** Plain-language description of what the chart shows, for screen readers. */
  summary: string;
  height?: string;
}

/**
 * Candlestick or line chart. `series` is the full ordered list; the common live tick only changes the last bar,
 * so that case uses `update()` and everything else (first load, new interval) uses `setData()`.
 * Colours come from the design tokens and are re-applied whenever the theme changes.
 */
export function CandleChart({ series, resetKey, mode = "candles", daily = false, summary, height }: Props) {
  const host = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi | null>(null);
  const bars = useRef<ISeriesApi<"Candlestick"> | ISeriesApi<"Line"> | null>(null);
  const shown = useRef<{ key: string; length: number; first: number } | null>(null);

  useEffect(() => {
    if (!host.current) return;
    const api = createChart(host.current, { autoSize: true, ...themeOptions(daily) });
    const candleColors = () => ({ upColor: token("--pos", "#047857"), downColor: token("--neg", "#a81a1a"), wickUpColor: token("--pos", "#047857"), wickDownColor: token("--neg", "#a81a1a") });
    bars.current =
      mode === "candles"
        ? api.addCandlestickSeries({ ...candleColors(), borderVisible: false })
        : api.addLineSeries({ color: token("--brand", "#0a7f61"), lineWidth: 2, priceLineVisible: true });
    chart.current = api;
    shown.current = null;

    const paint = () => {
      api.applyOptions(themeOptions(daily));
      if (mode === "candles") (bars.current as ISeriesApi<"Candlestick">).applyOptions(candleColors());
      else (bars.current as ISeriesApi<"Line">).applyOptions({ color: token("--brand", "#0a7f61") });
    };
    // Follow the theme: the class on <html> flips when the user toggles it or the system changes.
    const observer = new MutationObserver(paint);
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ["class"] });
    return () => {
      observer.disconnect();
      api.remove();
      chart.current = null;
      bars.current = null;
    };
  }, [mode, daily]);

  useEffect(() => {
    const target = bars.current;
    if (!target || series.length === 0) return;
    const last = series[series.length - 1]!;
    const prev = shown.current;
    const incremental = prev !== null && prev.key === resetKey && prev.first === series[0]!.time && (series.length === prev.length || series.length === prev.length + 1);
    if (mode === "candles") {
      const s = target as ISeriesApi<"Candlestick">;
      if (incremental) s.update(toBar(last));
      else {
        s.setData(series.map(toBar));
        chart.current?.timeScale().fitContent();
      }
    } else {
      const s = target as ISeriesApi<"Line">;
      if (incremental) s.update(toPoint(last));
      else {
        s.setData(series.map(toPoint));
        chart.current?.timeScale().fitContent();
      }
    }
    shown.current = { key: resetKey, length: series.length, first: series[0]!.time };
  }, [series, resetKey, mode, daily]);

  return <div ref={host} role="group" aria-roledescription="chart" aria-label={summary} className="w-full" style={{ height: height ?? "clamp(240px, 42vw, 420px)" }} />;
}

/** One sentence describing a series, for the chart's accessible name. */
export function describeSeries(name: string, bars: Bar[], unit = "bars"): string {
  if (bars.length === 0) return `${name}: no data yet.`;
  const first = bars[0]!;
  const last = bars[bars.length - 1]!;
  const change = last.close - first.open;
  const pct = first.open ? (change / first.open) * 100 : 0;
  const high = Math.max(...bars.map((b) => b.high));
  const low = Math.min(...bars.map((b) => b.low));
  return `${name}: ${bars.length} ${unit}. Opened at ${first.open.toFixed(2)}, last close ${last.close.toFixed(2)} (${change >= 0 ? "up" : "down"} ${Math.abs(pct).toFixed(2)} percent). High ${high.toFixed(2)}, low ${low.toFixed(2)}.`;
}
