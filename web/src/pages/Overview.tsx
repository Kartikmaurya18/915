import { memo, useCallback, useEffect, useMemo, useState } from "react";
import { Link, useLocation } from "react-router-dom";
import { ArrowRight, Zap } from "lucide-react";
import { usePaperState } from "../api/usePaperState";
import { useConfig } from "../api/useConfig";
import type { CandlesTopic, IndexTopic, OptionsTopic, StrikeRow } from "../api/types";
import { PriceChartCard } from "../components/PriceChartCard";
import { StraddleChart } from "../components/StraddleChart";
import { fmt, fmtCompact, fmtInt, fmtSigned, fmtTime } from "../lib/format";
import { useTopic } from "../realtime/RealtimeProvider";
import { distinctStrikes, focusStrike, selectionKey } from "../state/selection";
import { useSelection } from "../state/SelectionContext";
import { Badge } from "../ui/Badge";
import { Button } from "../ui/Button";
import { Card } from "../ui/Card";
import { cn } from "../ui/cn";
import { EmptyState, ErrorState } from "../ui/feedback";
import { Checkbox } from "../ui/fields";
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

      <OptionChain options={options.data} atm={atm?.strike ?? null} loading={!config || options.data === undefined} />

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

const WINDOW = 6; // strikes shown either side of the at-the-money strike unless expanded

interface RowProps {
  row: StrikeRow;
  ce: boolean;
  pe: boolean;
  onPick: (strike: number, type: "CE" | "PE") => void;
  onTrade: (strike: number) => void;
}

/** One strike. Memoised: a tick that changes three strikes re-renders three rows, not all of them. */
const ChainRow = memo(function ChainRow({ row, ce, pe, onPick, onTrade }: RowProps) {
  const bg = row.is_atm ? "bg-brand-soft" : "bg-surface";
  return (
    <tr>
      <td className={cn(TD, "w-px pr-0", bg)}>
        <Checkbox checked={ce} onChange={() => onPick(row.strike, "CE")} aria-label={`Select ${fmtInt(row.strike)} CE`} />
      </td>
      <td className={cn(TD, R, "cursor-pointer font-medium", bg, ce && "bg-brand-soft ring-1 ring-inset ring-brand")} onClick={() => onPick(row.strike, "CE")}>
        {fmt(row.call_ltp)}
      </td>
      <td className={cn(TD, R, "text-muted", bg)}>{fmt(row.call_delta, 3)}</td>
      <td className={cn(TD, R, "sticky left-0 z-1 font-semibold shadow-[1px_0_0_var(--line)]", bg)}>
        {fmtInt(row.strike)}
        {row.is_atm && <Badge tone="brand" className="ml-1.5 align-middle">ATM</Badge>}
      </td>
      <td className={cn(TD, R, "text-muted", bg)}>{fmt(row.put_delta, 3)}</td>
      <td className={cn(TD, R, "cursor-pointer font-medium", bg, pe && "bg-brand-soft ring-1 ring-inset ring-brand")} onClick={() => onPick(row.strike, "PE")}>
        {fmt(row.put_ltp)}
      </td>
      <td className={cn(TD, "w-px pr-0", bg)}>
        <Checkbox checked={pe} onChange={() => onPick(row.strike, "PE")} aria-label={`Select ${fmtInt(row.strike)} PE`} />
      </td>
      <td className={cn(TD, R, bg)}>{fmt(row.combined_value)}</td>
      <td className={cn(TD, R, "text-muted", bg)}>{fmtCompact(row.call_oi)}</td>
      <td className={cn(TD, R, "text-muted", bg)}>{fmtCompact(row.put_oi)}</td>
      <td className={cn(TD, bg)}>
        <button type="button" onClick={() => onTrade(row.strike)} aria-label={`Trade ${fmtInt(row.strike)}`} className="grid size-8 place-items-center rounded-control text-muted hover:bg-surface-2 hover:text-brand-fg">
          <Zap aria-hidden="true" className="size-4" />
        </button>
      </td>
    </tr>
  );
});

function OptionChain({ options, atm, loading }: { options: OptionsTopic | undefined; atm: number | null; loading: boolean }) {
  const sel = useSelection();
  const { openOrder } = useUI();
  const [showAll, setShowAll] = useState(false);
  const all = useMemo(() => [...(options?.strikes ?? [])].sort((a, b) => a.strike - b.strike), [options?.strikes]);
  const rows = useMemo(() => {
    if (showAll) return all;
    const at = all.findIndex((r) => r.is_atm || r.strike === atm);
    return at < 0 ? all : all.slice(Math.max(0, at - WINDOW), at + WINDOW + 1);
  }, [all, atm, showAll]);

  const expiry = options?.expiry ?? null;
  const underlying = options?.underlying ?? null;
  const toggle = sel.toggle;
  const onPick = useCallback((strike: number, optionType: "CE" | "PE") => toggle({ strike, optionType, expiry, underlying }), [toggle, expiry, underlying]);
  const onTrade = useCallback((strike: number) => openOrder({ strike, optionType: "CE" }), [openOrder]);
  const count = sel.selection.size;

  return (
    <Card
      id="option-chain"
      title="Option chain"
      description={expiry ? `Expiry ${expiry}. Tick CE or PE to build a selection; one is a single trade, several is multi-strike.` : "Waiting for the option chain…"}
      flush
      className="scroll-mt-20"
      actions={
        count > 0 && (
          <>
            <Button size="sm" onClick={sel.clear}>
              Clear
            </Button>
            <Link to="/trading" className="inline-flex min-h-8 items-center gap-1.5 rounded-control bg-brand px-3 text-sm font-medium text-on-brand hover:brightness-110">
              Trade {count} selected <ArrowRight aria-hidden="true" className="size-4" />
            </Link>
          </>
        )
      }
    >
      {loading ? (
        <div className="px-4 pb-4 sm:px-5"><div aria-busy="true" role="status" className="h-48 animate-pulse rounded-control bg-surface-2"><span className="sr-only">Loading option chain</span></div></div>
      ) : rows.length === 0 ? (
        <EmptyState title="No option chain yet">The chain appears once the market feed delivers it.</EmptyState>
      ) : (
        <div className="px-2 pb-3 sm:px-3">
          <TableWrap label="Option chain" className="max-h-128">
            <table className={TABLE}>
              <caption className="sr-only">Option chain by strike with call and put prices, deltas, combined value and open interest</caption>
              <thead>
                <tr>
                  <th className={TH}><span className="sr-only">Select call</span></th>
                  <th className={cn(TH, R)}>CE LTP</th>
                  <th className={cn(TH, R)}>CE Δ</th>
                  <th className={cn(TH, R, "sticky left-0 z-20")}>Strike</th>
                  <th className={cn(TH, R)}>PE Δ</th>
                  <th className={cn(TH, R)}>PE LTP</th>
                  <th className={TH}><span className="sr-only">Select put</span></th>
                  <th className={cn(TH, R)}>CE+PE</th>
                  <th className={cn(TH, R)}>CE OI</th>
                  <th className={cn(TH, R)}>PE OI</th>
                  <th className={TH}><span className="sr-only">Trade</span></th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <ChainRow key={r.strike} row={r} ce={sel.selection.has(selectionKey(r.strike, "CE"))} pe={sel.selection.has(selectionKey(r.strike, "PE"))} onPick={onPick} onTrade={onTrade} />
                ))}
              </tbody>
            </table>
          </TableWrap>
          {(all.length > rows.length || showAll) && (
            <div className="mt-2 px-2">
              <Button size="sm" onClick={() => setShowAll((v) => !v)}>
                {showAll ? "Show fewer strikes" : `Show all ${all.length} strikes`}
              </Button>
            </div>
          )}
        </div>
      )}
    </Card>
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
