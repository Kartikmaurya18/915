import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { CalendarX, ChevronLeft, ChevronRight } from "lucide-react";
import { api } from "../api/http";
import type { HistoricalSession, TradingDates } from "../api/types";
import { CandleChart, describeSeries } from "../components/CandleChart";
import { StraddleChart } from "../components/StraddleChart";
import { fmt, fmtDateTime, fmtInt, fmtSigned, fmtTime } from "../lib/format";
import { fmtDateDMY, nextAvailable, previousAvailable, snapToAvailable, todayIST } from "../lib/dates";
import { Badge, Signed } from "../ui/Badge";
import { Button } from "../ui/Button";
import { Card } from "../ui/Card";
import { cn } from "../ui/cn";
import { EmptyState, ErrorState, Skeleton } from "../ui/feedback";
import { Input } from "../ui/fields";
import { Metric, MetricGrid, PageHeader, R, TABLE, TD, TH, TableWrap } from "../ui/layout";
import { Segmented } from "../ui/Segmented";

/** A read-only look at one past trading day: price summary, candles, ATM history, a strike's straddle, and your trades. */
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
      <PageHeader title="Session history" description="Browse a past trading day. Read-only." actions={<Badge tone="neutral">Read-only</Badge>} />

      <Card>
        <div className="flex flex-wrap items-center gap-2">
          <Button disabled={!prev} onClick={() => void go("prev")} aria-label="Previous trading day">
            <ChevronLeft aria-hidden="true" className="size-4" />
            Previous
          </Button>
          <Input type="date" aria-label="Trading date" value={date ?? ""} max={today} onChange={(e) => void picked(e.target.value)} className="w-auto text-left" />
          <Button disabled={!next} onClick={() => void go("next")} aria-label="Next trading day">
            Next
            <ChevronRight aria-hidden="true" className="size-4" />
          </Button>
          <Button variant="ghost" disabled={date === today} onClick={() => void go("today")}>
            Today
          </Button>
          <strong className="num ml-1 text-base" title={date === today ? "Today, recorded so far" : "Past trading day"}>
            {fmtDateDMY(date)}
          </strong>
          {date === today && (
            <Link to="/" className="text-sm font-medium text-brand-fg underline-offset-2 hover:underline">
              Open the live view →
            </Link>
          )}
        </div>
        <p className="mt-2 text-xs text-muted">Previous and Next jump to the nearest day that actually has data.</p>
        {error && <ErrorState className="mt-3">{error}</ErrorState>}
      </Card>

      {loading && !session && (
        <div role="status" aria-busy="true" className="grid gap-3">
          <span className="sr-only">Loading</span>
          <Skeleton className="h-24 w-full" />
          <Skeleton className="h-72 w-full" />
        </div>
      )}
      {session && !session.has_data && (
        <Card>
          <EmptyState title="No data available for this trading session." icon={<CalendarX className="size-5" />}>
            Try another day. Previous and Next skip days without data.
          </EmptyState>
        </Card>
      )}
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
      <section aria-label="Session summary">
        <MetricGrid>
          <Metric label="Open" value={fmt(u.open)} />
          <Metric label="High" value={fmt(u.high)} />
          <Metric label="Low" value={fmt(u.low)} />
          <Metric label="Close" value={fmt(u.close)} sub={u.last_updated ? "Last update " + fmtTime(u.last_updated) : undefined} />
        </MetricGrid>
      </section>

      <Card
        title={`${u.symbol} candles`}
        actions={<Segmented label="Candle interval" size="sm" value={active} onChange={setInterval} options={intervals.map((m) => ({ value: m, label: `${m}m`, title: `${m} minute candles` }))} />}
      >
        <CandleChart series={series} resetKey={"history:" + date + ":" + active} summary={describeSeries(`${u.symbol} ${active} minute candles on ${fmtDateDMY(date)}`, series, "candles")} />
      </Card>

      <StraddleChart strike={strike} date={date} initial={session.straddle_candles} expiry={lastExpiry} options={session.available_strikes} onPick={setStrike} chip={session.available_strikes.length ? undefined : "No strikes recorded"} />

      <div className="grid gap-4 lg:grid-cols-2 lg:gap-5">
        <Card title="ATM history" description={`Last ${tail.length} of ${atm.length} snapshots`} flush>
          <div className="px-2 pb-3 sm:px-3">
            {tail.length === 0 ? (
              <EmptyState title="No data" />
            ) : (
              <TableWrap label="ATM history" className="max-h-96">
                <table className={TABLE}>
                  <caption className="sr-only">At-the-money snapshots, newest first</caption>
                  <thead>
                    <tr>
                      <th className={TH}>Time</th>
                      <th className={cn(TH, R)}>Strike</th>
                      <th className={cn(TH, R)}>CE</th>
                      <th className={cn(TH, R)}>PE</th>
                      <th className={cn(TH, R)}>Straddle</th>
                    </tr>
                  </thead>
                  <tbody>
                    {tail.map((r) => (
                      <tr key={r.timestamp}>
                        <td className={TD}>{fmtTime(r.timestamp)}</td>
                        <td className={cn(TD, R)}>{fmtInt(r.atm_strike)}</td>
                        <td className={cn(TD, R)}>{fmt(r.atm_call_ltp)}</td>
                        <td className={cn(TD, R)}>{fmt(r.atm_put_ltp)}</td>
                        <td className={cn(TD, R, "font-medium")}>{fmt(r.straddle_premium)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </TableWrap>
            )}
          </div>
        </Card>

        <Card title="Your paper trades" description={pt?.stats ? `${pt.stats.total_trades} trades · P&L ${fmtSigned(pt.stats.total_pnl)}` : undefined} flush>
          <div className="px-2 pb-3 sm:px-3">
            {(pt?.history?.length ?? 0) === 0 ? (
              <EmptyState title="No trades on this day" />
            ) : (
              <TableWrap label="Your paper trades" className="max-h-96">
                <table className={TABLE}>
                  <caption className="sr-only">Your simulated trades closed on this day</caption>
                  <thead>
                    <tr>
                      <th className={TH}>Contract</th>
                      <th className={cn(TH, R)}>Qty</th>
                      <th className={cn(TH, R)}>Entry</th>
                      <th className={cn(TH, R)}>Exit</th>
                      <th className={cn(TH, R)}>P&amp;L</th>
                      <th className={TH}>Reason</th>
                      <th className={TH}>Closed</th>
                    </tr>
                  </thead>
                  <tbody>
                    {pt.history.map((p) => (
                      <tr key={p.id}>
                        <td className={TD}>
                          <span className="font-medium">{fmtInt(p.strike)}</span> <Badge tone={p.option_type === "CE" ? "pos" : "neg"}>{p.option_type}</Badge> <Badge>{p.side}</Badge>
                        </td>
                        <td className={cn(TD, R)}>{fmtInt(p.quantity)}</td>
                        <td className={cn(TD, R)}>{fmt(p.entry_price)}</td>
                        <td className={cn(TD, R)}>{fmt(p.exit_price)}</td>
                        <td className={cn(TD, R)}>
                          <Signed value={p.pnl} />
                        </td>
                        <td className={TD}>{p.exit_reason ?? "—"}</td>
                        <td className={TD}>{fmtDateTime(p.exit_time)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </TableWrap>
            )}
          </div>
        </Card>
      </div>
    </>
  );
}
