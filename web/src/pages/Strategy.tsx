import { useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { AlertTriangle, ArrowRight, Plus, Trash2 } from "lucide-react";
import { api } from "../api/http";
import { useConfig } from "../api/useConfig";
import type { IndexTopic, OptionsTopic, PaperPosition } from "../api/types";
import { PayoffChart } from "../components/PayoffChart";
import { fmt, fmtInt, fmtSigned } from "../lib/format";
import { yearsToExpiry } from "../lib/bs";
import {
  TEMPLATES, buildFromTemplate, hasUnlimitedRisk, ltpOf, markToMarket, metrics, modelCurve, newLegId, payoffAt, repriceLeg, spotGrid,
  type OptionType, type Side, type StrategyLeg, type StrategyTemplate,
} from "../lib/strategy";
import { useTopic } from "../realtime/RealtimeProvider";
import { executeEntries, summarise } from "../state/execute";
import type { Selected } from "../state/selection";
import { useSelection } from "../state/SelectionContext";
import { Badge } from "../ui/Badge";
import { Button } from "../ui/Button";
import { Card } from "../ui/Card";
import { cn } from "../ui/cn";
import { Dialog } from "../ui/Dialog";
import { EmptyState, ErrorState, FormMessage } from "../ui/feedback";
import { Input, Select } from "../ui/fields";
import { Metric, MetricGrid, PageHeader, R, TABLE, TD, TH, TableWrap } from "../ui/layout";

export default function Strategy() {
  const { config, error } = useConfig();
  const underlying = config?.underlying ?? null;
  const options = useTopic<OptionsTopic>(underlying && `options:${underlying}`).data;
  const index = useTopic<IndexTopic>(underlying && `index:${underlying}`).data;
  const sel = useSelection();

  const [legs, setLegs] = useState<StrategyLeg[]>([]);
  const [templateId, setTemplateId] = useState<string | null>(null);
  const [builtExpiry, setBuiltExpiry] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [review, setReview] = useState(false);

  const rows = useMemo(() => [...(options?.strikes ?? [])].sort((a, b) => a.strike - b.strike), [options?.strikes]);
  const atm = index?.atm?.strike ?? null;
  const spot = index?.price ?? options?.underlying_ltp ?? null;
  const expiry = options?.expiry ?? null;
  const quantity = sel.defaultQuantity;

  const years = yearsToExpiry(expiry, Date.now());
  const m = useMemo(() => metrics(legs), [legs]);
  const grid = useMemo(() => (legs.length && spot ? spotGrid(legs, spot) : []), [legs, spot]);
  const expiryCurve = useMemo(() => grid.map((s) => payoffAt(legs, s)), [grid, legs]);
  const todayCurve = useMemo(() => (grid.length ? modelCurve(legs, rows, years, grid) : null), [grid, legs, rows, years]);
  const mtm = useMemo(() => markToMarket(legs, rows), [legs, rows]);
  const stale = builtExpiry !== null && expiry !== null && builtExpiry !== expiry;

  function apply(t: StrategyTemplate) {
    const built = buildFromTemplate(t, rows, atm, quantity);
    setTemplateId(t.id);
    setProblem(built.problem);
    if (!built.problem) {
      setLegs(built.legs);
      setBuiltExpiry(expiry);
    }
  }
  function addLeg() {
    const row = rows.find((r) => r.strike === atm) ?? rows[0];
    const premium = row ? ltpOf(row, "CE") : null;
    if (!row || premium === null) return setProblem("The option chain has no price to start a leg from yet.");
    setProblem(null);
    setTemplateId(null);
    setLegs((l) => [...l, { id: newLegId(), strike: row.strike, optionType: "CE", side: "BUY", quantity, premium }]);
    setBuiltExpiry((e) => e ?? expiry);
  }
  function changeLeg(id: string, change: Partial<Pick<StrategyLeg, "strike" | "optionType" | "side" | "quantity">>) {
    setTemplateId(null);
    setProblem(null);
    setLegs((all) =>
      all.map((l) => {
        if (l.id !== id) return l;
        const next = { ...l, ...change };
        // A new strike or type is a different contract: its premium is that contract's price now.
        return change.strike !== undefined || change.optionType !== undefined ? (repriceLeg(next, rows) ?? l) : next;
      }),
    );
  }
  function clear() {
    setLegs([]);
    setTemplateId(null);
    setBuiltExpiry(null);
    setProblem(null);
  }

  if (error) return <ErrorState>{error}</ErrorState>;
  const loading = !config || options === undefined;

  return (
    <>
      <PageHeader title="Strategy builder" description="Combine option legs, see what they pay at expiry and where they break even, then place them as simulated positions." />

      <Card title="Start from a strategy" description="Strikes are picked relative to the at-the-money strike and priced from the live chain. Edit any leg afterwards.">
        <div role="group" aria-label="Strategy presets" className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-4">
          {TEMPLATES.map((t) => (
            <button
              key={t.id}
              type="button"
              aria-pressed={templateId === t.id}
              disabled={loading}
              onClick={() => apply(t)}
              className={cn(
                "flex min-h-14 flex-col items-start justify-center rounded-control border px-3 py-2 text-left transition-colors disabled:opacity-60",
                templateId === t.id ? "border-brand bg-brand-soft" : "border-control bg-surface hover:bg-surface-2",
              )}
            >
              <span className="text-sm font-medium">{t.label}</span>
              <span className="text-xs text-muted">{t.view}</span>
            </button>
          ))}
        </div>
        {problem && <p role="alert" className="mt-3 text-sm font-medium text-neg">{problem}</p>}
      </Card>

      {legs.length === 0 ? (
        <EmptyState title="No legs yet">Pick a strategy above, or <button type="button" onClick={addLeg} className="font-medium text-brand-fg underline" disabled={loading}>add a single leg</button> and build your own.</EmptyState>
      ) : (
        <>
          {stale && (
            <p role="alert" className="flex items-start gap-2 rounded-control border border-line bg-warn-soft px-3 py-2 text-sm text-warn">
              <AlertTriangle aria-hidden="true" className="mt-0.5 size-4 shrink-0" />
              <span>The chain has moved to the {expiry} expiry since these legs were built. Rebuild them before trading; nothing will be placed from the old expiry.</span>
            </p>
          )}

          <section aria-label="Strategy summary">
            <MetricGrid columns={4}>
              <Metric label={m?.kind === "credit" ? "Net credit" : "Net debit"} value={m ? fmt(Math.abs(m.netPremium)) : "—"} sub="Premium paid or received at today's prices" />
              <Metric label="Max profit" value={m ? (m.maxProfit === null ? "Unlimited" : fmtSigned(m.maxProfit)) : "—"} tone="pos" sub="At expiry" />
              <Metric label="Max loss" value={m ? (m.maxLoss === null ? "Unlimited" : fmtSigned(m.maxLoss)) : "—"} tone="neg" sub={m?.riskReward ? `Reward : risk ${fmt(m.riskReward)} : 1` : "At expiry"} />
              <Metric label="Break-even" value={m && m.breakevens.length ? m.breakevens.map((b) => fmtInt(b)).join(" · ") : "—"} sub="Underlying level at expiry" />
            </MetricGrid>
            {hasUnlimitedRisk(m) && (
              <p className="mt-3 flex items-start gap-2 text-sm text-warn">
                <AlertTriangle aria-hidden="true" className="mt-0.5 size-4 shrink-0" />
                <span>This strategy has no limit on its loss. Paper trading does not model margin, so it would not be stopped for lack of funds the way a real account would.</span>
              </p>
            )}
          </section>

          <Card
            title="Payoff"
            description="The solid line is exact: what the legs pay if the underlying settles at each level. The dashed line is a model estimate of today's value."
            actions={<Badge tone={mtm === null ? "neutral" : mtm >= 0 ? "pos" : "neg"}>{mtm === null ? "Live P&L unavailable" : `Live P&L since added ${fmtSigned(mtm)}`}</Badge>}
          >
            {grid.length > 0 ? <PayoffChart spots={grid} expiry={expiryCurve} today={todayCurve} spot={spot} breakevens={m?.breakevens ?? []} /> : <p className="text-sm text-muted">Waiting for the underlying price.</p>}
            <p className="mt-3 text-xs text-muted">
              Expiry payoff ignores brokerage, taxes and slippage. "Today" re-prices every leg with Black-Scholes at its current implied volatility, so it is an estimate, not a market price.
              The live figure compares each leg's current traded price with the price it was added at.
            </p>
          </Card>

          <Card
            title="Legs"
            flush
            actions={
              <>
                <Button size="sm" onClick={addLeg}><Plus aria-hidden="true" className="size-4" />Add leg</Button>
                <Button size="sm" onClick={clear}>Clear</Button>
              </>
            }
          >
            <div className="px-2 pb-3 sm:px-3">
              <TableWrap label="Strategy legs">
                <table className={TABLE}>
                  <caption className="sr-only">The legs of the strategy with their side, strike, quantity, entry price and current price</caption>
                  <thead>
                    <tr>
                      <th className={TH}>Side</th>
                      <th className={TH}>Type</th>
                      <th className={TH}>Strike</th>
                      <th className={cn(TH, R)}>Qty</th>
                      <th className={cn(TH, R)}>Added at</th>
                      <th className={cn(TH, R)}>Now</th>
                      <th className={cn(TH, R)}>P&amp;L</th>
                      <th className={TH}><span className="sr-only">Remove</span></th>
                    </tr>
                  </thead>
                  <tbody>
                    {legs.map((l) => {
                      const now = ltpOf(rows.find((r) => r.strike === l.strike), l.optionType);
                      const raw = now === null ? null : (l.side === "BUY" ? 1 : -1) * (now - l.premium) * l.quantity;
                      const pnl = raw === null ? null : Math.round(raw * 100) / 100 || 0; // no "-0.00" from float noise
                      const name = `${l.side === "BUY" ? "Buy" : "Sell"} ${fmtInt(l.strike)} ${l.optionType}`;
                      return (
                        <tr key={l.id}>
                          <td className={TD}>
                            <Select aria-label={`Side of ${name}`} value={l.side} onChange={(e) => changeLeg(l.id, { side: e.target.value as Side })} className="min-h-8 w-24 py-1">
                              <option value="BUY">Buy</option>
                              <option value="SELL">Sell</option>
                            </Select>
                          </td>
                          <td className={TD}>
                            <Select aria-label={`Type of ${name}`} value={l.optionType} onChange={(e) => changeLeg(l.id, { optionType: e.target.value as OptionType })} className="min-h-8 w-20 py-1">
                              <option value="CE">CE</option>
                              <option value="PE">PE</option>
                            </Select>
                          </td>
                          <td className={TD}>
                            <Select aria-label={`Strike of ${name}`} value={l.strike} onChange={(e) => changeLeg(l.id, { strike: Number(e.target.value) })} className="min-h-8 w-28 py-1">
                              {!rows.some((r) => r.strike === l.strike) && <option value={l.strike}>{fmtInt(l.strike)} (off chain)</option>}
                              {rows.map((r) => <option key={r.strike} value={r.strike}>{fmtInt(r.strike)}{r.is_atm ? " · ATM" : ""}</option>)}
                            </Select>
                          </td>
                          <td className={cn(TD, R)}>
                            <Input aria-label={`Quantity of ${name}`} inputMode="numeric" value={l.quantity} onChange={(e) => changeLeg(l.id, { quantity: Math.max(1, Math.floor(Number(e.target.value)) || 1) })} className="min-h-8 w-20 py-1 text-right" />
                          </td>
                          <td className={cn(TD, R)}>{fmt(l.premium)}</td>
                          <td className={cn(TD, R)}>{fmt(now)}</td>
                          <td className={cn(TD, R, pnl !== null && pnl > 0 && "text-pos", pnl !== null && pnl < 0 && "text-neg")}>{pnl === null ? "—" : fmtSigned(pnl)}</td>
                          <td className={TD}>
                            <button type="button" aria-label={`Remove ${name}`} onClick={() => setLegs((all) => all.filter((x) => x.id !== l.id))} className="grid size-8 place-items-center rounded-control text-muted hover:bg-surface-2 hover:text-neg">
                              <Trash2 aria-hidden="true" className="size-4" />
                            </button>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </TableWrap>
              <div className="mt-3 flex flex-wrap items-center gap-3 px-2">
                <Button variant="primary" disabled={stale || loading} onClick={() => setReview(true)}>
                  Review simulated orders <ArrowRight aria-hidden="true" className="size-4" />
                </Button>
                <span className="text-xs text-muted">Nothing is placed until you confirm. No real order is ever sent.</span>
              </div>
            </div>
          </Card>
        </>
      )}

      <ReviewDialog open={review} onClose={() => setReview(false)} legs={legs} rows={rows} expiry={expiry} underlying={underlying} onPlaced={(ids) => setLegs((all) => all.filter((l) => !ids.has(l.id)))} netPremium={m?.netPremium ?? 0} />
    </>
  );
}

interface ReviewProps {
  open: boolean;
  onClose: () => void;
  legs: StrategyLeg[];
  rows: OptionsTopic["strikes"];
  expiry: string | null;
  underlying: string | null;
  netPremium: number;
  onPlaced: (ids: Set<string>) => void;
}

function ReviewDialog({ open, onClose, legs, rows, expiry, underlying, netPremium, onPlaced }: ReviewProps) {
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null);
  // One Idempotency-Key per distinct leg: a retry of the same review reuses it, a changed leg gets a new one.
  const nonces = useRef(new Map<string, { sig: string; nonce: string }>());

  const entries: Selected[] = legs.map((l) => {
    const sig = `${l.strike}|${l.optionType}|${l.side}|${l.quantity}|${expiry}`;
    let n = nonces.current.get(l.id);
    if (!n || n.sig !== sig) {
      n = { sig, nonce: crypto.randomUUID() };
      nonces.current.set(l.id, n);
    }
    return { key: l.id, strike: l.strike, optionType: l.optionType, side: l.side, quantity: l.quantity, expiry, underlying, nonce: n.nonce };
  });

  async function place() {
    if (busy) return;
    setBusy(true);
    setResult(null);
    const outcome = await executeEntries(entries, (e) =>
      api.post<PaperPosition>(
        "/api/paper/open",
        { option_type: e.optionType, strike: e.strike, side: e.side, quantity: e.quantity, expiry: e.expiry, underlying: e.underlying },
        { "Idempotency-Key": e.nonce },
      ),
    );
    onPlaced(new Set(outcome.succeeded.map((e) => e.key)));
    setResult(summarise(outcome));
    setBusy(false);
  }

  return (
    <Dialog open={open} onClose={() => { setResult(null); onClose(); }} label="Review simulated strategy orders" placement="drawer">
      <div className="flex h-full flex-col">
        <header className="border-b border-line p-4">
          <h2 className="text-lg font-semibold tracking-tight">Review simulated orders</h2>
          <p className="mt-1 text-sm text-muted">{legs.length} simulated position{legs.length === 1 ? "" : "s"}, each independent. No real order is placed.</p>
        </header>
        <div className="flex-1 overflow-y-auto p-4">
          {legs.length === 0 && result ? null : (
            <ul className="grid gap-2">
              {entries.map((e) => {
                const price = ltpOf(rows.find((r) => r.strike === e.strike), e.optionType);
                return (
                  <li key={e.key} className="flex items-center justify-between gap-3 rounded-control border border-line px-3 py-2 text-sm">
                    <span className="font-medium"><Badge tone={e.side === "BUY" ? "pos" : "neg"} className="mr-2">{e.side}</Badge>{fmtInt(e.strike)} {e.optionType} × {e.quantity}</span>
                    <span className="num text-muted">{price === null ? "no price" : `≈ ${fmt(price)}`}</span>
                  </li>
                );
              })}
            </ul>
          )}
          {legs.length > 0 && (
            <p className="num mt-4 text-sm">
              Estimated net {netPremium >= 0 ? "credit" : "debit"}: <strong>{fmt(Math.abs(netPremium))}</strong>
              <span className="block text-xs text-muted">Each leg fills at the live price when you confirm, which may differ from the figure above.</span>
            </p>
          )}
          {result && <div className="mt-4"><FormMessage ok={result.ok}>{result.text}</FormMessage></div>}
        </div>
        <footer className="flex flex-wrap items-center gap-2 border-t border-line p-4">
          {legs.length > 0 && <Button variant="primary" loading={busy} onClick={place}>Place {legs.length} simulated order{legs.length === 1 ? "" : "s"}</Button>}
          <Button onClick={() => { setResult(null); onClose(); }}>{legs.length === 0 && result?.ok ? "Done" : "Cancel"}</Button>
          {result?.ok && <Link to="/trading" className="ml-auto text-sm font-medium text-brand-fg hover:underline">View positions</Link>}
        </footer>
      </div>
    </Dialog>
  );
}
