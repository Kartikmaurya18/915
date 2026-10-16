import { useEffect, useMemo, useRef, useState } from "react";
import type { Bar, StraddleCandles, StraddleView, StrikeRow } from "../api/types";
import { useStraddleCandles } from "../api/useStraddle";
import { fmt, fmtInt, fmtSigned, pnlClass } from "../lib/format";
import { CandleChart } from "./CandleChart";

const VIEWS: { id: StraddleView; label: string }[] = [
  { id: "straddle", label: "Straddle" },
  { id: "ce", label: "CE" },
  { id: "pe", label: "PE" },
];
const INTERVALS = ["1", "5", "15"];

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
  const [interval, setIntervalKey] = useState("1");
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

  return (
    <section className="panel" aria-label="Straddle chart">
      <header className="panel-head">
        <h2>
          {date ? "Straddle" : "ATM straddle"} · {strike === null ? "—" : fmtInt(strike)}
          {expiry && <span className="muted"> · {expiry}</span>}
        </h2>
        <div className="row tight">
          {options && options.length > 1 && onPick ? (
            <select aria-label="Strike" value={strike ?? ""} onChange={(e) => onPick(Number(e.target.value))}>
              {options.map((o) => (
                <option key={o} value={o}>{fmtInt(o)}</option>
              ))}
            </select>
          ) : (
            chip && <span className="tag">{chip}</span>
          )}
          <div className="seg" role="tablist" aria-label="Series">
            {VIEWS.map((v) => (
              <button key={v.id} role="tab" aria-selected={v.id === view} className={v.id === view ? "seg-btn on" : "seg-btn"} onClick={() => setView(v.id)}>
                {v.label}
              </button>
            ))}
          </div>
          <div className="seg" role="tablist" aria-label="Straddle interval">
            {INTERVALS.map((i) => (
              <button key={i} role="tab" aria-selected={i === interval} className={i === interval ? "seg-btn on" : "seg-btn"} onClick={() => setIntervalKey(i)}>
                {i}m
              </button>
            ))}
          </div>
        </div>
      </header>
      <p className="straddle-now">
        <strong>{fmt(current)}</strong>{" "}
        {!date && change !== null && <span className={pnlClass(change)}>{fmtSigned(change)}</span>}
      </p>
      {strike === null || (noData && bars.length === 0) ? (
        <p className="empty">{strike === null ? "Waiting for an ATM strike…" : date ? "No candle data for this strike on this date." : "No candle data yet for this strike."}</p>
      ) : (
        <CandleChart series={bars} resetKey={`${strike}:${date ?? "live"}:${interval}:${view}`} />
      )}
      {error && <p className="form-error" role="alert">{error}</p>}
    </section>
  );
}
