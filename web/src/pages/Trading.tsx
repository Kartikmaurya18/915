import { useEffect, useRef, useState, type FormEvent } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { ShieldCheck, X, Zap } from "lucide-react";
import { api } from "../api/http";
import type { OptionsTopic, PaperConfig, PaperPosition, PaperState } from "../api/types";
import { useConfig } from "../api/useConfig";
import { usePaperState } from "../api/usePaperState";
import { fmt, fmtDateTime, fmtInt, fmtSigned } from "../lib/format";
import { useTopic } from "../realtime/RealtimeProvider";
import { executeEntries, summarise } from "../state/execute";
import { sorted, type Selected } from "../state/selection";
import { useSelection } from "../state/SelectionContext";
import { Badge, Signed } from "../ui/Badge";
import { Button } from "../ui/Button";
import { Card } from "../ui/Card";
import { cn } from "../ui/cn";
import { EmptyState, ErrorState, FormMessage } from "../ui/feedback";
import { Field, Input } from "../ui/fields";
import { Metric, MetricGrid, PageHeader, R, TABLE, TD, TH, TableWrap } from "../ui/layout";
import { Segmented } from "../ui/Segmented";
import { useUI } from "../ui/UIProvider";

export default function Trading() {
  const { state, error, refresh } = usePaperState();
  const { openOrder } = useUI();
  const [params, setParams] = useSearchParams();

  // Links like /trading?type=PE&strike=24800 open the order panel with that contract filled in.
  useEffect(() => {
    const type = params.get("type");
    const strike = params.get("strike");
    if (type === "CE" || type === "PE") {
      openOrder({ optionType: type, strike: strike ? Number(strike) : null });
      setParams({}, { replace: true });
    }
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <>
      <PageHeader
        title="Paper Trading"
        description="Practice trading with live prices. Nothing here is a real order."
        actions={
          <Button variant="primary" onClick={() => openOrder()}>
            <Zap aria-hidden="true" className="size-4" />
            New simulated order
          </Button>
        }
      />
      <p className="flex items-center gap-2 rounded-control bg-brand-soft px-3 py-2 text-sm text-brand-fg">
        <ShieldCheck aria-hidden="true" className="size-4 shrink-0" />
        Simulated trading only. Prices come from the live feed; no real orders are placed and no real money moves.
      </p>
      {error && !state && <ErrorState>{error}</ErrorState>}
      <Summary state={state} />
      <SelectedContracts onDone={refresh} />
      <ConfigForm config={state?.config} onDone={refresh} />
      <Positions title="Open positions" rows={state?.open_positions ?? []} onClose={refresh} open loading={!state} />
      <Positions title="History" rows={state?.history ?? []} onClose={refresh} loading={!state} />
    </>
  );
}

function Summary({ state }: { state: PaperState | null }) {
  const p = state?.pnl_summary;
  const s = state?.stats;
  const loading = !state;
  const tone = !p || p.total_pnl === 0 ? "neutral" : p.total_pnl > 0 ? "pos" : "neg";
  return (
    <section aria-label="Paper trading summary" className="grid gap-3">
      <MetricGrid>
        <Metric label="Total P&L" value={p ? fmtSigned(p.total_pnl) : "—"} tone={tone} delta={p?.total_pnl_percent} deltaPercent loading={loading} sub={p ? `Realised ${fmtSigned(p.realized_pnl)} · Unrealised ${fmtSigned(p.unrealized_pnl)}` : undefined} />
        <Metric label="Open positions" value={p ? p.open_position_count : "—"} loading={loading} sub={p ? `Value ${fmt(p.total_position_value)}` : undefined} />
        <Metric label="Win rate" value={s ? `${fmt(s.win_rate_percent, 1)}%` : "—"} loading={loading} sub={s ? `${s.wins}W · ${s.losses}L of ${s.total_trades}` : undefined} />
        <Metric label="Turnover" value={p ? fmt(p.total_turnover) : "—"} loading={loading} sub={p ? `Buy ${fmt(p.buy_turnover)} · Sell ${fmt(p.sell_turnover)}` : undefined} />
      </MetricGrid>
      <MetricGrid columns={3}>
        <Metric label="Max drawdown" value={s ? fmt(s.max_drawdown) : "—"} loading={loading} />
        <Metric label="Average win" value={s ? fmt(s.average_win) : "—"} loading={loading} />
        <Metric label="Average loss" value={s ? fmt(s.average_loss) : "—"} loading={loading} />
      </MetricGrid>
    </section>
  );
}

function SelectedContracts({ onDone }: { onDone: () => Promise<void> }) {
  const sel = useSelection();
  const { config } = useConfig();
  const options = useTopic<OptionsTopic>(config ? "options:" + config.underlying : null).data;
  const [busy, setBusy] = useState<string | null>(null); // a contract key, or "all"
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null);
  const entries = sorted(sel.selection);

  const openOne = (e: Selected) =>
    api.post<PaperPosition>(
      "/api/paper/open",
      { option_type: e.optionType, strike: e.strike, side: e.side, quantity: e.quantity, expiry: e.expiry, underlying: e.underlying },
      { "Idempotency-Key": e.nonce },
    );

  async function run(which: Selected[], token: string) {
    if (busy || which.length === 0) return;
    setBusy(token);
    setMessage(null);
    const result = await executeEntries(which, openOne);
    sel.remove(result.succeeded.map((e) => e.key)); // failures stay selected so they can be fixed or retried
    setMessage(summarise(result));
    setBusy(null);
    await onDone();
  }

  return (
    <Card
      title="Selected contracts"
      description="Chosen in the option chain. One contract is a single trade; several is a multi-strike trade."
      flush
      actions={
        <>
          <label className="flex items-center gap-2 text-sm text-muted">
            Default qty
            <Input
              inputMode="numeric"
              value={sel.defaultQuantity}
              onChange={(e) => sel.setDefaultQuantity(Math.max(1, Math.floor(Number(e.target.value)) || 1))}
              className="min-h-8 w-20 py-1 text-right"
            />
          </label>
          {entries.length > 0 && (
            <Button size="sm" onClick={sel.clear}>
              Clear
            </Button>
          )}
          <Button size="sm" variant="primary" disabled={entries.length === 0 || busy !== null} loading={busy === "all"} onClick={() => void run(entries, "all")}>
            {busy === "all" ? "Executing…" : entries.length ? "Execute " + entries.length + " selected" : "Execute selected"}
          </Button>
        </>
      }
    >
      <div className="px-2 pb-3 sm:px-3">
        {entries.length === 0 ? (
          <EmptyState title="No contracts selected">
            Tick CE or PE boxes in the{" "}
            <Link to="/" className="font-medium text-brand-fg underline">
              option chain
            </Link>{" "}
            to add them here.
          </EmptyState>
        ) : (
          <TableWrap label="Selected contracts">
            <table className={TABLE}>
              <caption className="sr-only">Contracts selected for simulated trading</caption>
              <thead>
                <tr>
                  <th className={cn(TH, R)}>Strike</th>
                  <th className={TH}>Type</th>
                  <th className={cn(TH, R)}>LTP</th>
                  <th className={cn(TH, R)}>Δ</th>
                  <th className={TH}>Expiry</th>
                  <th className={TH}>Side</th>
                  <th className={cn(TH, R)}>Qty</th>
                  <th className={TH}>
                    <span className="sr-only">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {entries.map((e) => {
                  const row = options?.strikes.find((r) => r.strike === e.strike);
                  const ce = e.optionType === "CE";
                  return (
                    <tr key={e.key}>
                      <td className={cn(TD, R)}>{fmtInt(e.strike)}</td>
                      <td className={TD}>
                        <Badge tone={ce ? "pos" : "neg"}>{e.optionType}</Badge>
                      </td>
                      <td className={cn(TD, R)}>{fmt(ce ? row?.call_ltp : row?.put_ltp)}</td>
                      <td className={cn(TD, R, "text-muted")}>{fmt(ce ? row?.call_delta : row?.put_delta, 3)}</td>
                      <td className={TD}>{e.expiry ?? "—"}</td>
                      <td className={TD}>
                        <Segmented
                          label={`Side for ${e.strike} ${e.optionType}`}
                          size="sm"
                          value={e.side}
                          onChange={(side) => sel.update(e.key, { side })}
                          options={[
                            { value: "BUY", label: "Buy" },
                            { value: "SELL", label: "Sell" },
                          ]}
                        />
                      </td>
                      <td className={cn(TD, R)}>
                        <QuantityInput value={e.quantity} onCommit={(q) => sel.update(e.key, { quantity: q })} label={"Quantity for " + e.strike + " " + e.optionType} />
                      </td>
                      <td className={TD}>
                        <div className="flex items-center gap-1.5">
                          <Button size="sm" disabled={busy !== null} onClick={() => void run([e], e.key)}>
                            {busy === e.key ? "…" : "Execute"}
                          </Button>
                          <button type="button" aria-label={`Remove ${fmtInt(e.strike)} ${e.optionType}`} onClick={() => sel.remove([e.key])} className="grid size-8 place-items-center rounded-control text-muted hover:bg-surface-2 hover:text-neg">
                            <X aria-hidden="true" className="size-4" />
                          </button>
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </TableWrap>
        )}
        {message && (
          <div className="mt-3 px-2">
            <FormMessage ok={message.ok}>{message.text}</FormMessage>
          </div>
        )}
      </div>
    </Card>
  );
}

/** Lets the user clear the field while typing; only a valid whole number above zero is committed. */
function QuantityInput({ value, onCommit, label }: { value: number; onCommit: (q: number) => void; label: string }) {
  const [text, setText] = useState(String(value));
  useEffect(() => setText(String(value)), [value]);
  return (
    <Input
      className="min-h-8 w-20 py-1 text-right"
      inputMode="numeric"
      aria-label={label}
      value={text}
      onChange={(e) => {
        setText(e.target.value);
        const q = Number(e.target.value);
        if (Number.isInteger(q) && q > 0) onCommit(q);
      }}
      onBlur={() => setText(String(value))}
    />
  );
}

function ConfigForm({ config, onDone }: { config: PaperConfig | undefined; onDone: () => Promise<void> }) {
  const [tp, setTp] = useState("");
  const [sl, setSl] = useState("");
  const [tpAmt, setTpAmt] = useState("");
  const [slAmt, setSlAmt] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null);
  const loaded = useRef(false);

  // Fill the form once from the saved values; never overwrite what the user is typing.
  useEffect(() => {
    if (!config || loaded.current) return;
    loaded.current = true;
    setTp(String(config.take_profit_percent));
    setSl(String(config.stop_loss_percent));
    setTpAmt(config.take_profit_amount === null ? "" : String(config.take_profit_amount));
    setSlAmt(config.stop_loss_amount === null ? "" : String(config.stop_loss_amount));
  }, [config]);

  const num = (v: string) => (v.trim() === "" ? null : Number(v));

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setMessage(null);
    try {
      await api.post("/api/paper/config", { take_profit_percent: num(tp), stop_loss_percent: num(sl), take_profit_amount: num(tpAmt), stop_loss_amount: num(slAmt) });
      setMessage({ ok: true, text: "Saved. Applies to positions opened from now on." });
      await onDone();
    } catch (err) {
      setMessage({ ok: false, text: err instanceof Error ? err.message : "Could not save" });
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card title="Take profit and stop loss" description="Positions close automatically when either threshold is reached.">
      <form onSubmit={submit} className="grid gap-4">
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <Field label="Take profit %">{(p) => <Input {...p} inputMode="decimal" value={tp} onChange={(e) => setTp(e.target.value)} />}</Field>
          <Field label="Stop loss %">{(p) => <Input {...p} inputMode="decimal" value={sl} onChange={(e) => setSl(e.target.value)} />}</Field>
          <Field label="Profit target ₹" hint="Optional">{(p) => <Input {...p} inputMode="decimal" value={tpAmt} onChange={(e) => setTpAmt(e.target.value)} />}</Field>
          <Field label="Loss limit ₹" hint="Optional">{(p) => <Input {...p} inputMode="decimal" value={slAmt} onChange={(e) => setSlAmt(e.target.value)} />}</Field>
        </div>
        <div className="flex flex-wrap items-center gap-3">
          <Button type="submit" loading={busy}>
            {busy ? "Saving…" : "Save"}
          </Button>
          {message && <FormMessage ok={message.ok}>{message.text}</FormMessage>}
        </div>
      </form>
    </Card>
  );
}

function Positions({ title, rows, onClose, open = false, loading }: { title: string; rows: PaperPosition[]; onClose: () => Promise<void>; open?: boolean; loading: boolean }) {
  const [closing, setClosing] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function close(id: string) {
    setClosing(id);
    setError(null);
    try {
      await api.post(`/api/paper/close/${encodeURIComponent(id)}`);
    } catch (err) {
      // 404 means it was already closed (for example by a take-profit just now): not an error to show.
      const status = (err as { status?: number }).status;
      if (status !== 404) setError(err instanceof Error ? err.message : "Could not close the position");
    } finally {
      setClosing(null);
      await onClose();
    }
  }

  return (
    <Card title={title} description={`${rows.length} ${rows.length === 1 ? "position" : "positions"}`} flush>
      <div className="px-2 pb-3 sm:px-3">
        {error && <ErrorState className="mx-2 mb-3">{error}</ErrorState>}
        {loading ? (
          <div role="status" aria-busy="true" className="mx-2 h-24 animate-pulse rounded-control bg-surface-2">
            <span className="sr-only">Loading</span>
          </div>
        ) : rows.length === 0 ? (
          <EmptyState title={open ? "No open positions" : "No closed trades yet"}>{open ? "Use New simulated order, or select contracts in the option chain." : "Closed positions appear here."}</EmptyState>
        ) : (
          <TableWrap label={title} className="max-h-112">
            <table className={TABLE}>
              <caption className="sr-only">{title}</caption>
              <thead>
                <tr>
                  <th className={TH}>Contract</th>
                  <th className={cn(TH, R)}>Qty</th>
                  <th className={cn(TH, R)}>Entry</th>
                  <th className={cn(TH, R)}>{open ? "Now" : "Exit"}</th>
                  <th className={cn(TH, R)}>P&amp;L</th>
                  <th className={cn(TH, open && R)}>{open ? "TP / SL" : "Reason"}</th>
                  <th className={TH}>{open ? <span className="sr-only">Close</span> : "Closed"}</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((p) => (
                  <tr key={p.id}>
                    <td className={TD}>
                      <span className="font-medium">{fmtInt(p.strike)}</span> <Badge tone={p.option_type === "CE" ? "pos" : "neg"}>{p.option_type}</Badge> <Badge>{p.side}</Badge>
                    </td>
                    <td className={cn(TD, R)}>{fmtInt(p.quantity)}</td>
                    <td className={cn(TD, R)}>{fmt(p.entry_price)}</td>
                    <td className={cn(TD, R)}>{fmt(open ? p.current_price : p.exit_price)}</td>
                    <td className={cn(TD, R)}>
                      <Signed value={p.pnl} /> <span className="text-xs text-muted">({fmtSigned(p.pnl_percent)}%)</span>
                    </td>
                    {open ? <td className={cn(TD, R)}>{fmt(p.take_profit_price)} / {fmt(p.stop_loss_price)}</td> : <td className={TD}>{p.exit_reason ?? "—"}</td>}
                    <td className={TD}>
                      {open ? (
                        <Button size="sm" disabled={closing === p.id} onClick={() => void close(p.id)}>
                          {closing === p.id ? "Closing…" : "Close"}
                        </Button>
                      ) : (
                        fmtDateTime(p.exit_time)
                      )}
                    </td>
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
