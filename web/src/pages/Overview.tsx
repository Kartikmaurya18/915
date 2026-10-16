import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { StraddleChart } from "../components/StraddleChart";
import { distinctStrikes, focusStrike, selectionKey } from "../state/selection";
import { useSelection } from "../state/SelectionContext";
import { useConfig } from "../api/useConfig";
import type { CandlesTopic, IndexTopic, OptionsTopic } from "../api/types";
import { CandleChart } from "../components/CandleChart";
import { fmt, fmtCompact, fmtInt, fmtTime } from "../lib/format";
import { useTopic } from "../realtime/RealtimeProvider";

export default function Overview() {
  const { config, error } = useConfig();
  const underlying = config?.underlying ?? null;
  const intervals = config?.candle_intervals?.length ? config.candle_intervals : [1, 5, 15];
  const [interval, setIntervalMinutes] = useState(1);

  const index = useTopic<IndexTopic>(underlying && `index:${underlying}`);
  const options = useTopic<OptionsTopic>(underlying && `options:${underlying}`);
  const candles = useTopic<CandlesTopic>(underlying && `candles:${underlying}:${interval}m`);

  if (error) return <p className="form-error" role="alert">{error}</p>;
  if (!config) return <p className="muted">Loading…</p>;

  const idx = index.data;
  const atm = idx?.atm ?? null;

  return (
    <>
      <section className="metrics" aria-label="Key figures">
        <Metric label={`${config.underlying} spot`} value={fmt(idx?.price)} sub={idx ? `Tick ${fmtTime(idx.timestamp)}` : "Waiting for data"} />
        <Metric label="ATM strike" value={fmtInt(atm?.strike)} sub={atm ? `${atm.method} · Δ from spot ${fmt(atm.distance_from_spot)}` : undefined} />
        <Metric label="ATM straddle" value={fmt(atm?.straddle_premium)} sub={atm ? `CE ${fmt(atm.call_ltp)} · PE ${fmt(atm.put_ltp)}` : undefined} />
        <Metric label="Feed" value={idx?.provider?.name ?? "—"} sub={idx ? `${idx.status} · session ${idx.data_quality?.market_session ?? "—"}` : undefined} />
      </section>

      <section className="panel">
        <header className="panel-head">
          <h2>Candlestick</h2>
          <div className="seg" role="tablist" aria-label="Candle interval">
            {intervals.map((m) => (
              <button key={m} role="tab" aria-selected={m === interval} className={m === interval ? "seg-btn on" : "seg-btn"} onClick={() => setIntervalMinutes(m)}>
                {m}m
              </button>
            ))}
          </div>
        </header>
        <CandleChart series={candles.data?.series ?? []} resetKey={`${underlying}:${interval}`} />
      </section>

      <OptionChain options={options.data} atm={atm?.strike ?? null} />

      <StraddleSection options={options.data} atm={atm?.strike ?? null} />

      <RecentCandles candles={candles.data} interval={interval} />
    </>
  );
}

function Metric({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="metric">
      <span className="metric-label">{label}</span>
      <strong className="metric-value">{value}</strong>
      {sub && <span className="metric-sub">{sub}</span>}
    </div>
  );
}

const WINDOW = 6; // strikes shown either side of the at-the-money strike unless expanded

function OptionChain({ options, atm }: { options: OptionsTopic | undefined; atm: number | null }) {
  const sel = useSelection();
  const [showAll, setShowAll] = useState(false);
  const all = useMemo(() => [...(options?.strikes ?? [])].sort((a, b) => a.strike - b.strike), [options?.strikes]);
  const rows = useMemo(() => {
    if (showAll) return all;
    const at = all.findIndex((r) => r.is_atm || r.strike === atm);
    if (at < 0) return all;
    return all.slice(Math.max(0, at - WINDOW), at + WINDOW + 1);
  }, [all, atm, showAll]);

  const pick = (strike: number, optionType: "CE" | "PE") =>
    sel.toggle({ strike, optionType, expiry: options?.expiry ?? null, underlying: options?.underlying ?? null });
  const count = sel.selection.size;

  return (
    <section className="panel">
      <header className="panel-head">
        <h2>Option chain</h2>
        <div className="row tight">
          <span className="muted">{options ? `Expiry ${options.expiry}` : ""}</span>
          {count > 0 && (
            <>
              <button className="btn btn-small" onClick={sel.clear}>Clear</button>
              <Link className="btn btn-small btn-primary" to="/trading">Trade {count} selected →</Link>
            </>
          )}
        </div>
      </header>
      <p className="hint">Tick the CE / PE boxes (or click a price) to add contracts to your paper-trading selection. One is a single trade; several is a multi-strike trade.</p>
      <div className="table-wrap">
        <table className="data">
          <thead>
            <tr>
              <th aria-label="Select CE" />
              <th className="r">CE LTP</th>
              <th className="r">CE Δ</th>
              <th className="r">Strike</th>
              <th className="r">PE Δ</th>
              <th className="r">PE LTP</th>
              <th aria-label="Select PE" />
              <th className="r">CE+PE</th>
              <th className="r">CE OI</th>
              <th className="r">PE OI</th>
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 && (
              <tr>
                <td colSpan={10} className="empty">Waiting for the option chain…</td>
              </tr>
            )}
            {rows.map((r) => {
              const ce = sel.selection.has(selectionKey(r.strike, "CE"));
              const pe = sel.selection.has(selectionKey(r.strike, "PE"));
              return (
                <tr key={r.strike} className={r.is_atm ? "atm" : undefined}>
                  <td className="chk"><input type="checkbox" checked={ce} onChange={() => pick(r.strike, "CE")} aria-label={`Select ${r.strike} CE`} /></td>
                  <td className={`r call clickable${ce ? " picked" : ""}`} onClick={() => pick(r.strike, "CE")}>{fmt(r.call_ltp)}</td>
                  <td className="r muted">{fmt(r.call_delta, 3)}</td>
                  <td className="r strike">{fmtInt(r.strike)}</td>
                  <td className="r muted">{fmt(r.put_delta, 3)}</td>
                  <td className={`r put clickable${pe ? " picked" : ""}`} onClick={() => pick(r.strike, "PE")}>{fmt(r.put_ltp)}</td>
                  <td className="chk"><input type="checkbox" checked={pe} onChange={() => pick(r.strike, "PE")} aria-label={`Select ${r.strike} PE`} /></td>
                  <td className="r">{fmt(r.combined_value)}</td>
                  <td className="r muted">{fmtCompact(r.call_oi)}</td>
                  <td className="r muted">{fmtCompact(r.put_oi)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {all.length > rows.length || showAll ? (
        <button className="btn btn-small" onClick={() => setShowAll((v) => !v)}>
          {showAll ? "Show fewer strikes" : `Show all ${all.length} strikes`}
        </button>
      ) : null}
    </section>
  );
}

/** The straddle chart follows the selection: ATM by default, the one selected strike, or the user's pick. */
function StraddleSection({ options, atm }: { options: OptionsTopic | undefined; atm: number | null }) {
  const sel = useSelection();
  const strike = focusStrike(sel.selection, atm, sel.pickedStrike);
  const strikes = distinctStrikes(sel.selection);
  const row = strike === null ? null : (options?.strikes.find((r) => r.strike === strike) ?? null);
  const chip = strikes.length === 0 ? "Following ATM" : strikes.length === 1 ? "Tracking selected strike" : undefined;
  return (
    <StraddleChart
      strike={strike}
      liveRow={row}
      expiry={options?.expiry ?? null}
      chip={chip}
      options={strikes.length > 1 ? strikes : undefined}
      onPick={sel.setPickedStrike}
    />
  );
}

function RecentCandles({ candles, interval }: { candles: CandlesTopic | undefined; interval: number }) {
  const recent = useMemo(() => [...(candles?.series ?? [])].slice(-12).reverse(), [candles?.series]);
  return (
    <section className="panel">
      <header className="panel-head">
        <h2>Recent {interval}m candles</h2>
      </header>
      <div className="table-wrap">
        <table className="data">
          <thead>
            <tr>
              <th>Time</th>
              <th className="r">Open</th>
              <th className="r">High</th>
              <th className="r">Low</th>
              <th className="r">Close</th>
            </tr>
          </thead>
          <tbody>
            {recent.length === 0 && (
              <tr>
                <td colSpan={5} className="empty">No candles yet</td>
              </tr>
            )}
            {recent.map((c) => (
              <tr key={c.open_time}>
                <td>{fmtTime(c.open_time)}{!c.is_complete && <span className="tag live"> live</span>}</td>
                <td className="r">{fmt(c.open)}</td>
                <td className="r">{fmt(c.high)}</td>
                <td className="r">{fmt(c.low)}</td>
                <td className="r">{fmt(c.close)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
