import { useEffect, useMemo, useRef, useState } from "react";
import { Crosshair } from "lucide-react";
import type { Bar, StraddleCandles, StraddleView, StrikeRow } from "../api/types";
import { useStraddleCandles } from "../api/useStraddle";
import { fmt, fmtInt } from "../lib/format";
import { Badge, DeltaBadge } from "../ui/Badge";
import { Card } from "../ui/Card";
import { EmptyState, ErrorState } from "../ui/feedback";
import { Select } from "../ui/fields";
import { Segmented } from "../ui/Segmented";
import { CandleChart, describeSeries } from "./CandleChart";

const VIEWS: { value: StraddleView; label: string; title: string }[] = [
  { value: "straddle", label: "Straddle", title: "Straddle: call plus put" },
  { value: "ce", label: "CE", title: "Call" },
  { value: "pe", label: "PE", title: "Put" },
];
const INTERVALS = ["1", "5", "15"] as const;

interface Props {
  /** The strike to chart, or null if there is none yet. */
  strike: number | null;
  /** A past trading date ("YYYY-MM-DD"). Omit for live data. */
  date?: string;
  /** Data already fetched by the page (the history bundle), shown without another request. */
  initial?: StraddleCandles | null;
  /** Live chain row for the strike, to show the current value between candle refreshes. */
  liveRow?: StrikeRow | null;
  expiry?: string | null;
  /** Short note next to the title, e.g. "Following ATM". */
  chip?: string;
  /** If given, the user can choose the strike from these. */
  options?: number[];
  onPick?: (strike: number) => void;
}

function liveValue(row: StrikeRow | null | undefined, view: StraddleView): number | null {
  if (!row) return null;
  return view === "straddle" ? row.combined_value : view === "ce" ? row.call_ltp : row.put_ltp;
}

export function StraddleChart({ strike, date, initial, liveRow, expiry, chip, options, onPick }: Props) {
  const [view, setView] = useState<StraddleView>("straddle");
  const [interval, setIntervalKey] = useState<(typeof INTERVALS)[number]>("1");
  const { data, noData, error } = useStraddleCandles(strike, date, initial);

  const bars: Bar[] = useMemo(() => data?.by_interval?.[interval]?.[view] ?? [], [data, interval, view]);

  // Current value and its change since the previous sample (live), or the day's last close (history).
  const current = date ? (bars.length ? bars[bars.length - 1]!.close : null) : (liveValue(liveRow, view) ?? (bars.length ? bars[bars.length - 1]!.close : null));
  const previous = useRef<number | null>(null);
  const [change, setChange] = useState<number | null>(null);
  useEffect(() => {
    previous.current = null;
    setChange(null);
  }, [strike, view, date]);
  useEffect(() => {
    if (date || current === null) return;
    if (previous.current !== null && previous.current !== current) setChange(current - previous.current);
    previous.current = current;
  }, [current, date]);

  const viewName = VIEWS.find((v) => v.value === view)!.title;
  return (
    <Card
      aria-label="Straddle chart"
      title={
        <span className="flex flex-wrap items-center gap-2">
          {date ? "Straddle" : "ATM straddle"} · {strike === null ? "—" : fmtInt(strike)}
          {expiry && <span className="text-sm font-normal text-muted">{expiry}</span>}
          {!options && chip && <Badge tone="brand">{chip}</Badge>}
        </span>
      }
      actions={
        <>
          {options && options.length > 1 && onPick && (
            <Select aria-label="Strike" value={strike ?? ""} onChange={(e) => onPick(Number(e.target.value))} className="min-h-8 w-auto py-1">
              {options.map((o) => (
                <option key={o} value={o}>
                  {fmtInt(o)}
                </option>
              ))}
            </Select>
          )}
          <Segmented label="Series" size="sm" value={view} onChange={setView} options={VIEWS} />
          <Segmented label="Straddle interval" size="sm" value={interval} onChange={setIntervalKey} options={INTERVALS.map((i) => ({ value: i, label: `${i}m`, title: `${i} minute candles` }))} />
        </>
      }
    >
      <div className="mb-2 flex items-baseline gap-2">
        <span className="num text-2xl font-semibold tracking-tight">{fmt(current)}</span>
        {!date && change !== null && <DeltaBadge value={change} />}
      </div>
      {strike === null || (noData && bars.length === 0) ? (
        <EmptyState title={strike === null ? "Waiting for an ATM strike" : "No candles for this strike yet"} icon={<Crosshair className="size-5" />}>
          {date ? "There is no candle data for this strike on this date." : "Candles appear after the first few ticks."}
        </EmptyState>
      ) : (
        <CandleChart series={bars} resetKey={`${strike}:${date ?? "live"}:${interval}:${view}`} summary={describeSeries(`${viewName} for strike ${fmtInt(strike)}, ${interval} minute candles`, bars, "candles")} height="clamp(220px, 36vw, 340px)" />
      )}
      {error && <ErrorState className="mt-3">{error}</ErrorState>}
    </Card>
  );
}
