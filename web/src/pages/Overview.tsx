import { useEffect, useMemo } from "react";
import { Link, useLocation } from "react-router-dom";
import { ArrowRight, Zap } from "lucide-react";
import { usePaperState } from "../api/usePaperState";
import { useConfig } from "../api/useConfig";
import type { CandlesTopic, IndexTopic, OptionsTopic } from "../api/types";
import { OptionChainCard } from "../components/OptionChainCard";
import { PriceChartCard } from "../components/PriceChartCard";
import { StraddleChart } from "../components/StraddleChart";
import { StrangleChart } from "../components/StrangleChart";
import { fmt, fmtInt, fmtSigned, fmtTime } from "../lib/format";
import { useTopic } from "../realtime/RealtimeProvider";
import { distinctStrikes, focusStrike } from "../state/selection";
import { useSelection } from "../state/SelectionContext";
import { Badge } from "../ui/Badge";
import { Button } from "../ui/Button";
import { Card } from "../ui/Card";
import { cn } from "../ui/cn";
import { EmptyState, ErrorState } from "../ui/feedback";
import { Metric, MetricGrid, PageHeader, R, TABLE, TD, TH, TableWrap } from "../ui/layout";
import { useUI } from "../ui/UIProvider";

export default function Overview() {
  const { config, error } = useConfig();
  const underlying = config?.underlying ?? null;
  const intervals = config?.candle_intervals?.length ? config.candle_intervals : [1, 5, 15];

  const index = useTopic<IndexTopic>(underlying && `index:${underlying}`);
  const options = useTopic<OptionsTopic>(underlying && `options:${underlying}`);
  const candles = useTopic<CandlesTopic>(underlying && `candles:${underlying}:${intervals[0] ?? 1}m`);
  const { openOrder } = useUI();
  const location = useLocation();

  // "/#option-chain" (from the command palette) scrolls to the chain.
  useEffect(() => {
    if (location.hash && options.data) document.getElementById(location.hash.slice(1))?.scrollIntoView({ block: "start" });
  }, [location.hash, options.data === undefined]); // eslint-disable-line react-hooks/exhaustive-deps

  if (error) return <ErrorState>{error}</ErrorState>;

  const idx = index.data;
  const atm = idx?.atm ?? null;
  const waiting = !config || !idx;

  return (
    <>
      <PageHeader
        title="Overview"
        description="Live market view. Prices stream in real time; trading here is a simulation."
        actions={
          <Button variant="primary" onClick={() => openOrder()}>
            <Zap aria-hidden="true" className="size-4" />
            New simulated order
          </Button>
        }
      />

      <section aria-label="Market summary">
        <MetricGrid>
          <Metric label={`${underlying ?? "Index"} spot`} value={fmt(idx?.price)} sub={idx ? `Tick ${fmtTime(idx.timestamp)}` : "Waiting for data"} loading={waiting} />
          <Metric label="ATM strike" value={fmtInt(atm?.strike)} sub={atm ? `${atm.method} · ${fmt(atm.distance_from_spot)} from spot` : undefined} loading={waiting} />
          <Metric label="ATM straddle" value={fmt(atm?.straddle_premium)} sub={atm ? `CE ${fmt(atm.call_ltp)} · PE ${fmt(atm.put_ltp)}` : undefined} loading={waiting} />
          <Metric label="Feed" value={idx?.provider?.name ?? "—"} sub={idx ? `${idx.status} · session ${idx.data_quality?.market_session ?? "—"}` : undefined} loading={waiting} />
        </MetricGrid>
      </section>

      <PaperSummary />

      {underlying && <PriceChartCard underlying={underlying} intervals={intervals} />}

      <StraddleSection options={options.data} atm={atm?.strike ?? null} />

      <StrangleChart rows={options.data?.strikes ?? []} atm={atm?.strike ?? null} expiry={options.data?.expiry ?? null} />

      <OptionChainCard options={options.data} atm={atm?.strike ?? null} loading={!config || options.data === undefined} />

      <RecentCandles candles={candles.data} interval={intervals[0] ?? 1} />
    </>
  );
}

/** A compact view of the signed-in user's own simulated book, linking to the full page. */
function PaperSummary() {
  const { state, error } = usePaperState();
  const p = state?.pnl_summary;
  const s = state?.stats;
  return (
    <section aria-label="Your paper trading summary" className="grid gap-3">
      <div className="flex items-center justify-between">
        <h2 className="text-base font-semibold tracking-tight">Your simulated book</h2>
        <Link to="/trading" className="inline-flex min-h-8 items-center gap-1 rounded-control text-sm font-medium text-brand-fg hover:underline">
          Open Paper Trading <ArrowRight aria-hidden="true" className="size-4" />
        </Link>
      </div>
      {error && !state ? (
        <ErrorState>{error}</ErrorState>
      ) : (
        <MetricGrid>
          <Metric label="Total P&L" value={p ? fmtSigned(p.total_pnl) : "—"} tone={!p || p.total_pnl === 0 ? "neutral" : p.total_pnl > 0 ? "pos" : "neg"} delta={p?.total_pnl_percent} deltaPercent loading={!state} sub={p ? `Realised ${fmtSigned(p.realized_pnl)} · Unrealised ${fmtSigned(p.unrealized_pnl)}` : undefined} />
          <Metric label="Open positions" value={p ? p.open_position_count : "—"} loading={!state} sub={p ? `Value ${fmt(p.total_position_value)}` : undefined} />
          <Metric label="Win rate" value={s ? `${fmt(s.win_rate_percent, 1)}%` : "—"} loading={!state} sub={s ? `${s.wins}W · ${s.losses}L of ${s.total_trades}` : undefined} />
          <Metric label="Turnover" value={p ? fmt(p.total_turnover) : "—"} loading={!state} sub={p ? `Buy ${fmt(p.buy_turnover)} · Sell ${fmt(p.sell_turnover)}` : undefined} />
        </MetricGrid>
      )}
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
  return <StraddleChart strike={strike} liveRow={row} expiry={options?.expiry ?? null} chip={chip} options={strikes.length > 1 ? strikes : undefined} onPick={sel.setPickedStrike} />;
}

function RecentCandles({ candles, interval }: { candles: CandlesTopic | undefined; interval: number }) {
  const recent = useMemo(() => [...(candles?.series ?? [])].slice(-12).reverse(), [candles?.series]);
  return (
    <Card title={`Recent ${interval}m candles`} flush>
      <div className="px-2 pb-3 sm:px-3">
        {recent.length === 0 ? (
          <EmptyState title="No candles yet">They appear as soon as the first ticks arrive.</EmptyState>
        ) : (
          <TableWrap label="Recent candles" className="max-h-80">
            <table className={TABLE}>
              <caption className="sr-only">The most recent candles, newest first</caption>
              <thead>
                <tr>
                  <th className={TH}>Time</th>
                  <th className={cn(TH, R)}>Open</th>
                  <th className={cn(TH, R)}>High</th>
                  <th className={cn(TH, R)}>Low</th>
                  <th className={cn(TH, R)}>Close</th>
                </tr>
              </thead>
              <tbody>
                {recent.map((c) => (
                  <tr key={c.open_time}>
                    <td className={TD}>
                      {fmtTime(c.open_time)}
                      {!c.is_complete && <Badge tone="pos" className="ml-2">live</Badge>}
                    </td>
                    <td className={cn(TD, R)}>{fmt(c.open)}</td>
                    <td className={cn(TD, R)}>{fmt(c.high)}</td>
                    <td className={cn(TD, R)}>{fmt(c.low)}</td>
                    <td className={cn(TD, R)}>{fmt(c.close)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </TableWrap>
        )}
      </div>
    </Card>
  );
}
