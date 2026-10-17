import { useState } from "react";
import { Plus, Trash2, Zap } from "lucide-react";
import type { IndexTopic, OptionsTopic } from "../api/types";
import { useConfig } from "../api/useConfig";
import { fmt, fmtInt } from "../lib/format";
import { MAX_LISTS, addItem, createList, deleteList, itemLabel, quoteFor, removeItem, renameList, setActive, type WatchItem } from "../lib/watchlist";
import { useTopic } from "../realtime/RealtimeProvider";
import { useWatchlists } from "../state/useWatchlists";
import { Badge, DeltaBadge } from "../ui/Badge";
import { Button } from "../ui/Button";
import { Card } from "../ui/Card";
import { EmptyState } from "../ui/feedback";
import { Input, Select } from "../ui/fields";
import { R, TABLE, TD, TH, TableWrap } from "../ui/layout";
import { useUI } from "../ui/UIProvider";

const WHY: Record<string, string> = {
  "other-expiry": "Different expiry",
  "no-price": "No price",
  "other-underlying": "Not streaming",
};

/** Named lists of contracts with live prices and the move since each was added. */
export function WatchlistPanel() {
  const { config } = useConfig();
  const underlying = config?.underlying ?? null;
  const options = useTopic<OptionsTopic>(underlying && `options:${underlying}`).data;
  const index = useTopic<IndexTopic>(underlying && `index:${underlying}`).data;
  const { state, update } = useWatchlists();
  const { openOrder } = useUI();
  const list = state.lists.find((l) => l.id === state.active)!;
  const [renaming, setRenaming] = useState(false);
  const [name, setName] = useState("");
  const [strike, setStrike] = useState("");
  const [type, setType] = useState<"CE" | "PE">("CE");

  const strikes = (options?.strikes ?? []).map((r) => r.strike).sort((a, b) => a - b);
  const chosen = strike !== "" ? Number(strike) : (index?.atm?.strike ?? strikes[0] ?? null);

  function addOption() {
    if (!underlying || chosen === null || !options) return;
    const row = options.strikes.find((r) => r.strike === chosen);
    const price = row ? (type === "CE" ? row.call_ltp : row.put_ltp) : null;
    update((s) => addItem(s, list.id, { kind: "option", underlying, strike: chosen, optionType: type, expiry: options.expiry, addedPrice: price }));
  }
  function addIndex() {
    if (!underlying) return;
    update((s) => addItem(s, list.id, { kind: "index", underlying, addedPrice: index?.price ?? null }));
  }
  function trade(i: WatchItem) {
    if (i.kind === "option") openOrder({ optionType: i.optionType, strike: i.strike });
    else openOrder();
  }

  return (
    <Card
      aria-label="Watchlist"
      title="Watchlist"
      description="Contracts you follow. Prices come from the live chain; a contract from another expiry shows no price rather than a wrong one."
      flush
      actions={
        <>
          <Select aria-label="Choose watchlist" value={state.active} onChange={(e) => update((s) => setActive(s, e.target.value))} className="min-h-8 w-auto max-w-44 py-1">
            {state.lists.map((l) => <option key={l.id} value={l.id}>{l.name} ({l.items.length})</option>)}
          </Select>
          <Button size="sm" disabled={state.lists.length >= MAX_LISTS} onClick={() => update((s) => createList(s, ""))}><Plus aria-hidden="true" className="size-4" />New list</Button>
        </>
      }
    >
      <div className="px-4 pb-4 sm:px-5">
        <div className="mb-3 flex flex-wrap items-end gap-2">
          <label className="grid gap-1 text-sm">
            <span className="text-muted">Strike</span>
            <Select value={chosen ?? ""} onChange={(e) => setStrike(e.target.value)} className="min-h-8 w-28 py-1" disabled={strikes.length === 0}>
              {strikes.map((k) => <option key={k} value={k}>{fmtInt(k)}</option>)}
            </Select>
          </label>
          <label className="grid gap-1 text-sm">
            <span className="text-muted">Type</span>
            <Select value={type} onChange={(e) => setType(e.target.value as "CE" | "PE")} className="min-h-8 w-20 py-1"><option value="CE">CE</option><option value="PE">PE</option></Select>
          </label>
          <Button size="sm" onClick={addOption} disabled={chosen === null}>Add contract</Button>
          <Button size="sm" onClick={addIndex} disabled={!underlying}>Add {underlying ?? "index"}</Button>
          <span className="ml-auto flex gap-2">
            {renaming ? (
              <form onSubmit={(e) => { e.preventDefault(); update((s) => renameList(s, list.id, name)); setRenaming(false); }} className="flex gap-2">
                <Input aria-label="List name" autoFocus value={name} onChange={(e) => setName(e.target.value)} maxLength={40} className="min-h-8 w-40 py-1" />
                <Button size="sm" type="submit">Save</Button>
              </form>
            ) : (
              <Button size="sm" variant="ghost" onClick={() => { setName(list.name); setRenaming(true); }}>Rename</Button>
            )}
            <Button size="sm" variant="ghost" disabled={state.lists.length <= 1} onClick={() => update((s) => deleteList(s, list.id))}>Delete list</Button>
          </span>
        </div>

        {list.items.length === 0 ? (
          <EmptyState title="Nothing watched yet">Add a contract above, or select contracts in the option chain and choose Watch.</EmptyState>
        ) : (
          <TableWrap label={`${list.name} watchlist`} className="max-h-80">
            <table className={TABLE}>
              <caption className="sr-only">Watched instruments with last price and change since they were added</caption>
              <thead>
                <tr><th className={TH}>Instrument</th><th className={`${TH} ${R}`}>Price</th><th className={`${TH} ${R}`}>Since added</th><th className={TH}><span className="sr-only">Actions</span></th></tr>
              </thead>
              <tbody>
                {list.items.map((i) => {
                  const q = quoteFor(i, index, options);
                  const change = q.price !== null && i.addedPrice ? ((q.price - i.addedPrice) / i.addedPrice) * 100 : null;
                  return (
                    <tr key={i.id}>
                      <td className={TD}>{itemLabel(i)}{i.kind === "option" && i.expiry && <span className="ml-2 text-xs text-muted">{i.expiry}</span>}</td>
                      <td className={`${TD} ${R} font-medium`}>{q.price === null ? <Badge tone="warn">{WHY[q.state]}</Badge> : fmt(q.price)}</td>
                      <td className={`${TD} ${R}`}>{change === null ? "—" : <DeltaBadge value={change} percent />}</td>
                      <td className={`${TD} whitespace-nowrap`}>
                        <button type="button" aria-label={`Trade ${itemLabel(i)}`} onClick={() => trade(i)} className="inline-grid size-8 place-items-center rounded-control text-muted hover:bg-surface-2 hover:text-brand-fg"><Zap aria-hidden="true" className="size-4" /></button>
                        <button type="button" aria-label={`Remove ${itemLabel(i)}`} onClick={() => update((s) => removeItem(s, list.id, i.id))} className="inline-grid size-8 place-items-center rounded-control text-muted hover:bg-surface-2 hover:text-neg"><Trash2 aria-hidden="true" className="size-4" /></button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </TableWrap>
        )}
      </div>
    </Card>
  );
}
