import { useMemo, useState } from "react";
import { LineChart } from "lucide-react";
import type { Bar, CandlesTopic } from "../api/types";
import { useDailyBars } from "../api/useDailyBars";
import { fmt, fmtDateTime, fmtDay } from "../lib/format";
import { RANGES, inRange, type Range } from "../lib/daily";
import { useTopic } from "../realtime/RealtimeProvider";
import { Segmented } from "../ui/Segmented";
import { DeltaBadge } from "../ui/Badge";
import { Card } from "../ui/Card";
import { EmptyState, ErrorState, Skeleton } from "../ui/feedback";
import { R, TABLE, TD, TH, TableWrap } from "../ui/layout";
import { CandleChart, describeSeries } from "./CandleChart";

type Mode = "candles" | "line";

const RANGE_NAMES: Record<Range, string> = { "1D": "today", "1W": "1 week", "1M": "1 month", "1Y": "1 year", ALL: "all history" };

/**
 * The main price chart. "1D" is the live session (1m / 5m / 15m candles streaming over the WebSocket); the longer
 * ranges show one bar per trading day built from stored history. Candles or a line; a text summary and a data table
 * make the same information available without seeing the chart.
 */
export function PriceChartCard({ underlying, intervals }: { underlying: string; intervals: number[] }) {
  const [range, setRange] = useState<Range>("1D");
  const [interval, setIntervalMinutes] = useState(intervals[0] ?? 1);
  const [mode, setMode] = useState<Mode>("candles");
  const [tableOpen, setTableOpen] = useState(false);

  const live = range === "1D";
  const candles = useTopic<CandlesTopic>(live ? `candles:${underlying}:${interval}m` : null).data;
  const daily = useDailyBars(!live);

  const bars: Bar[] = useMemo(() => (live ? (candles?.series ?? []) : inRange(daily.bars, range)), [live, candles?.series, daily.bars, range]);
  const loading = live ? candles === undefined : daily.loading;
  const first = bars[0];
  const last = bars[bars.length - 1];
  const pct = first && last && first.open ? ((last.close - first.open) / first.open) * 100 : null;
  const name = `${underlying} ${live ? `${interval} minute` : "daily"} ${mode === "candles" ? "candles" : "line"}, ${RANGE_NAMES[range]}`;

  return (
    <Card
      title={
        <span className="flex flex-wrap items-center gap-2">
          {underlying}
          {last && <span className="num text-base font-normal text-muted">{fmt(last.close)}</span>}
          {pct !== null && <DeltaBadge value={pct} percent />}
        </span>
      }
      description={live ? "Live session" : `One bar per trading day, ${RANGE_NAMES[range]}`}
      actions={
        <>
          {live && (
            <Segmented
              label="Candle interval"
              size="sm"
              value={String(interval)}
              onChange={(v) => setIntervalMinutes(Number(v))}
              options={intervals.map((m) => ({ value: String(m), label: `${m}m`, title: `${m} minute candles` }))}
            />
          )}
          <Segmented
            label="Chart type"
            size="sm"
            value={mode}
            onChange={setMode}
            options={[
              { value: "candles", label: "Candles" },
              { value: "line", label: "Line" },
            ]}
          />
          <Segmented label="Time range" size="sm" value={range} onChange={setRange} options={RANGES.map((r) => ({ value: r, label: r, title: RANGE_NAMES[r] }))} />
        </>
      }
    >
      {daily.error && !live ? (
        <ErrorState>{daily.error}</ErrorState>
      ) : loading ? (
        <Skeleton className="h-[clamp(240px,42vw,420px)] w-full" />
      ) : bars.length === 0 ? (
        <EmptyState title="No price history yet" icon={<LineChart className="size-5" />}>
          {live ? "Candles appear as soon as the first ticks arrive." : "History builds up as trading days are recorded."}
        </EmptyState>
      ) : (
        <CandleChart series={bars} resetKey={`${underlying}:${range}:${interval}`} mode={mode} daily={!live} summary={describeSeries(name, bars, live ? "candles" : "days")} />
      )}

      {bars.length > 0 && (
        <details className="mt-3" onToggle={(e) => setTableOpen((e.currentTarget as HTMLDetailsElement).open)}>
          <summary className="inline-flex min-h-8 cursor-pointer items-center rounded-control text-sm font-medium text-brand-fg">View as a table</summary>
          {tableOpen && (
            <TableWrap label="Latest bars" className="mt-2 max-h-64">
              <table className={TABLE}>
                <caption className="sr-only">Latest {Math.min(15, bars.length)} bars of {name}</caption>
                <thead>
                  <tr>
                    <th className={TH}>{live ? "Time" : "Date"}</th>
                    <th className={`${TH} ${R}`}>Open</th>
                    <th className={`${TH} ${R}`}>High</th>
                    <th className={`${TH} ${R}`}>Low</th>
                    <th className={`${TH} ${R}`}>Close</th>
                  </tr>
                </thead>
                <tbody>
                  {[...bars].slice(-15).reverse().map((b) => (
                    <tr key={b.time}>
                      <td className={TD}>{live ? fmtDateTime(b.time * 1000) : fmtDay(b.time)}</td>
                      <td className={`${TD} ${R}`}>{fmt(b.open)}</td>
                      <td className={`${TD} ${R}`}>{fmt(b.high)}</td>
                      <td className={`${TD} ${R}`}>{fmt(b.low)}</td>
                      <td className={`${TD} ${R}`}>{fmt(b.close)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </TableWrap>
          )}
        </details>
      )}
    </Card>
  );
}
