import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
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

      <div className="grid-2">
        <OptionChain options={options.data} />
        <RecentCandles candles={candles.data} interval={interval} />
      </div>
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

function OptionChain({ options }: { options: OptionsTopic | undefined }) {
  const rows = options?.strikes ?? [];
  return (
    <section className="panel">
      <header className="panel-head">
        <h2>Option chain</h2>
        <span className="muted">{options ? `Expiry ${options.expiry}` : ""}</span>
      </header>
      <div className="table-wrap">
        <table className="data">
          <thead>
            <tr>
              <th className="r">CE OI</th>
              <th className="r">CE LTP</th>
              <th className="r">Strike</th>
              <th className="r">PE LTP</th>
              <th className="r">PE OI</th>
              <th aria-label="Trade" />
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 && (
              <tr>
                <td colSpan={6} className="empty">Waiting for the option chain…</td>
              </tr>
            )}
            {rows.map((r) => (
              <tr key={r.strike} className={r.is_atm ? "atm" : undefined}>
                <td className="r">{fmtCompact(r.call_oi)}</td>
                <td className="r call">{fmt(r.call_ltp)}</td>
                <td className="r strike">{fmtInt(r.strike)}</td>
                <td className="r put">{fmt(r.put_ltp)}</td>
                <td className="r">{fmtCompact(r.put_oi)}</td>
                <td className="actions">
                  <Link className="chip call" to={`/trading?type=CE&strike=${r.strike}`} aria-label={`Trade ${r.strike} CE`}>CE</Link>
                  <Link className="chip put" to={`/trading?type=PE&strike=${r.strike}`} aria-label={`Trade ${r.strike} PE`}>PE</Link>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
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
