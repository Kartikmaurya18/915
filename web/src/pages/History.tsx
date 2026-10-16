import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/http";
import type { HistoricalSession, TradingDates } from "../api/types";
import { CandleChart } from "../components/CandleChart";
import { StraddleChart } from "../components/StraddleChart";
import { fmt, fmtDateTime, fmtInt, fmtSigned, fmtTime, pnlClass } from "../lib/format";
import { fmtDateDMY, nextAvailable, previousAvailable, snapToAvailable, todayIST } from "../lib/dates";

/** A read-only look at one past trading session: price summary, candles, ATM history, a strike's straddle, and your trades. */
export default function History() {
  const [dates, setDates] = useState<TradingDates | null>(null);
  const [date, setDate] = useState<string | null>(null);
  const [session, setSession] = useState<HistoricalSession | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [strike, setStrike] = useState<number | null>(null);
  const [interval, setIntervalKey] = useState("1");
  const request = useRef(0);

  const loadDates = useCallback(async (): Promise<TradingDates | null> => {
    try {
      const d = await api.get<TradingDates>("/api/history/dates");
      setDates(d);
      return d;
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load trading dates");
      return null;
    }
  }, []);

  // First visit: open on the most recent day that has data.
  useEffect(() => {
    void loadDates().then((d) => {
      if (d) setDate(d.dates[0] ?? d.today ?? todayIST());
    });
  }, [loadDates]);

  // Load the session whenever the date changes; ignore answers that arrive after a newer request.
  useEffect(() => {
    if (!date) return;
    const mine = ++request.current;
    setLoading(true);
    setError(null);
    api
      .get<HistoricalSession>("/api/history/session?date=" + encodeURIComponent(date))
      .then((s) => {
        if (request.current !== mine) return;
        setSession(s);
        setStrike(s.straddle_candles?.strike ?? s.available_strikes[0] ?? null);
      })
      .catch((err: unknown) => {
        if (request.current === mine) {
          setSession(null);
          setError(err instanceof Error ? err.message : "Could not load this session");
        }
      })
      .finally(() => {
        if (request.current === mine) setLoading(false);
      });
  }, [date]);

  const today = dates?.today ?? todayIST();
  const list = dates?.dates ?? [];
  const prev = date ? previousAvailable(list, today, date) : null;
  const next = date ? nextAvailable(list, today, date) : null;

  // Refresh the date list first so a long-open page notices days that gained data since it loaded.
  async function go(kind: "prev" | "next" | "today") {
    const fresh = (await loadDates()) ?? dates;
    if (!fresh || !date) return;
    const t = fresh.today;
    const target = kind === "today" ? t : kind === "prev" ? previousAvailable(fresh.dates, t, date) : nextAvailable(fresh.dates, t, date);
    if (target) setDate(target);
  }
  async function picked(value: string) {
    if (!value) return;
    const fresh = (await loadDates()) ?? dates;
    if (fresh) setDate(snapToAvailable(fresh.dates, fresh.today, value));
  }

  return (
    <>
      <section className="panel">
        <header className="panel-head">
          <h2>Session history</h2>
          <span className="tag">Read-only</span>
        </header>
        <div className="datebar">
          <button className="btn" disabled={!prev} onClick={() => void go("prev")} aria-label="Previous trading day">← Previous</button>
          <input type="date" aria-label="Trading date" value={date ?? ""} max={today} onChange={(e) => void picked(e.target.value)} />
          <button className="btn" disabled={!next} onClick={() => void go("next")} aria-label="Next trading day">Next →</button>
          <button className="btn btn-quiet" disabled={date === today} onClick={() => void go("today")}>Today</button>
          <strong className="datelabel" title={date === today ? "Today, recorded so far" : "Past trading day"}>{fmtDateDMY(date)}</strong>
          {date === today && <Link to="/">Open the live view →</Link>}
        </div>
        <p className="hint">Previous and Next jump to the nearest day that actually has data.</p>
        {error && <p className="form-error" role="alert">{error}</p>}
      </section>

      {loading && !session && <p className="muted">Loading…</p>}
      {session && !session.has_data && <div className="panel notice"><p>No data available for this trading session.</p></div>}
      {session && session.has_data && <Session session={session} interval={interval} setInterval={setIntervalKey} strike={strike} setStrike={setStrike} date={session.trading_date} />}
    </>
  );
}

function Session({ session, interval, setInterval, strike, setStrike, date }: { session: HistoricalSession; interval: string; setInterval: (i: string) => void; strike: number | null; setStrike: (s: number) => void; date: string }) {
  const u = session.underlying;
  const intervals = useMemo(() => Object.keys(session.candles ?? {}).sort((a, b) => Number(a) - Number(b)), [session.candles]);
  const active = intervals.includes(interval) ? interval : (intervals[0] ?? "1");
  const series = session.candles?.[active] ?? [];
  const atm = session.option_chain_snapshots ?? [];
  const tail = useMemo(() => atm.slice(-100).reverse(), [atm]);
  const lastExpiry = atm.length ? atm[atm.length - 1]!.expiry : null;
  const pt = session.paper_trading;

  return (
    <>
      <section className="metrics" aria-label="Session summary">
        <Tile label="Open" value={fmt(u.open)} />
        <Tile label="High" value={fmt(u.high)} />
        <Tile label="Low" value={fmt(u.low)} />
        <Tile label="Close" value={fmt(u.close)} sub={u.last_updated ? "Last update " + fmtTime(u.last_updated) : undefined} />
      </section>

      <section className="panel">
        <header className="panel-head">
          <h2>{u.symbol} candles</h2>
          <div className="seg" role="tablist" aria-label="Candle interval">
            {intervals.map((m) => (
              <button key={m} role="tab" aria-selected={m === active} className={m === active ? "seg-btn on" : "seg-btn"} onClick={() => setInterval(m)}>
                {m}m
              </button>
            ))}
          </div>
        </header>
        <CandleChart series={series} resetKey={"history:" + date + ":" + active} />
      </section>

      <StraddleChart strike={strike} date={date} initial={session.straddle_candles} expiry={lastExpiry} options={session.available_strikes} onPick={setStrike} chip={session.available_strikes.length ? undefined : "No strikes recorded"} />

      <div className="grid-2">
        <section className="panel">
          <header className="panel-head">
            <h2>ATM history</h2>
            <span className="muted">last {tail.length} of {atm.length}</span>
          </header>
          <div className="table-wrap">
            <table className="data">
              <thead>
                <tr><th>Time</th><th className="r">Strike</th><th className="r">CE</th><th className="r">PE</th><th className="r">Straddle</th></tr>
              </thead>
              <tbody>
                {tail.length === 0 && <tr><td colSpan={5} className="empty">No data</td></tr>}
                {tail.map((r) => (
                  <tr key={r.timestamp}>
                    <td>{fmtTime(r.timestamp)}</td>
                    <td className="r">{fmtInt(r.atm_strike)}</td>
                    <td className="r call">{fmt(r.atm_call_ltp)}</td>
                    <td className="r put">{fmt(r.atm_put_ltp)}</td>
                    <td className="r">{fmt(r.straddle_premium)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>

        <section className="panel">
          <header className="panel-head">
            <h2>Your paper trades</h2>
            <span className="muted">{pt?.stats ? pt.stats.total_trades + " trades · P&L " + fmtSigned(pt.stats.total_pnl) : ""}</span>
          </header>
          <div className="table-wrap">
            <table className="data">
              <thead>
                <tr><th>Contract</th><th className="r">Qty</th><th className="r">Entry</th><th className="r">Exit</th><th className="r">P&amp;L</th><th>Reason</th><th>Closed</th></tr>
              </thead>
              <tbody>
                {(pt?.history?.length ?? 0) === 0 && <tr><td colSpan={7} className="empty">No trades on this day</td></tr>}
                {pt?.history?.map((p) => (
                  <tr key={p.id}>
                    <td><span className={p.option_type === "CE" ? "call" : "put"}>{fmtInt(p.strike)} {p.option_type}</span> <span className="tag">{p.side}</span></td>
                    <td className="r">{fmtInt(p.quantity)}</td>
                    <td className="r">{fmt(p.entry_price)}</td>
                    <td className="r">{fmt(p.exit_price)}</td>
                    <td className={"r " + pnlClass(p.pnl)}>{fmtSigned(p.pnl)}</td>
                    <td>{p.exit_reason ?? "—"}</td>
                    <td>{fmtDateTime(p.exit_time)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      </div>
    </>
  );
}

function Tile({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="metric">
      <span className="metric-label">{label}</span>
      <strong className="metric-value">{value}</strong>
      {sub && <span className="metric-sub">{sub}</span>}
    </div>
  );
}
