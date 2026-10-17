import { memo, useCallback, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { ArrowDown, ArrowRight, ArrowUp, Columns3, Zap } from "lucide-react";
import type { OptionsTopic, StrikeRow } from "../api/types";
import {
  COLUMNS, DEFAULT_COLUMNS, DEFAULT_SORT, cellValue, filterRows, moneyness, nextSort, orderedColumns, sortRows,
  type ColumnDef, type ColumnKey, type Side, type Sort, type Window,
} from "../lib/chain";
import { fmt, fmtInt } from "../lib/format";
import { addItem } from "../lib/watchlist";
import { selectionKey } from "../state/selection";
import { useSelection } from "../state/SelectionContext";
import { useWatchlists } from "../state/useWatchlists";
import { Badge } from "../ui/Badge";
import { Button } from "../ui/Button";
import { Card } from "../ui/Card";
import { cn } from "../ui/cn";
import { EmptyState } from "../ui/feedback";
import { Checkbox, Input } from "../ui/fields";
import { R, TABLE, TD, TH, TableWrap } from "../ui/layout";
import { Segmented } from "../ui/Segmented";
import { useUI } from "../ui/UIProvider";

const STORAGE_KEY = "md_chain_columns";

function loadColumns(): ColumnKey[] {
  try {
    const raw = JSON.parse(localStorage.getItem(STORAGE_KEY) ?? "null") as unknown;
    if (Array.isArray(raw)) {
      const known = new Set<string>(COLUMNS.map((c) => c.key));
      const kept = raw.filter((k): k is ColumnKey => typeof k === "string" && known.has(k));
      if (kept.length > 0 && kept.includes("ltp")) return kept;
    }
  } catch {
    /* storage unavailable or corrupt: use the defaults */
  }
  return [...DEFAULT_COLUMNS];
}
function saveColumns(cols: ColumnKey[]): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(cols));
  } catch {
    /* a preference only; the chain works without it */
  }
}

const GREEKS_NOTE: Record<string, string> = {
  provider: "Greeks and IV from the data provider.",
  calculated: "Greeks and IV calculated here from prices (Black-Scholes); the provider sent none.",
  "provider+calculated": "Greeks and IV: the provider's where it sent them, calculated here for the rest.",
};

interface RowProps {
  row: StrikeRow;
  spot: number | null;
  callCols: ColumnDef[];
  putCols: ColumnDef[];
  ce: boolean;
  pe: boolean;
  onPick: (strike: number, type: "CE" | "PE") => void;
  onTrade: (strike: number) => void;
}

/** One strike. Memoised: a tick that changes three strikes re-renders three rows, not all of them. */
const ChainRow = memo(function ChainRow({ row, spot, callCols, putCols, ce, pe, onPick, onTrade }: RowProps) {
  const atm = row.is_atm;
  const cell = (side: Side, col: ColumnDef) => {
    const m = moneyness(row.strike, spot, side, false);
    const selected = col.key === "ltp" && (side === "call" ? ce : pe);
    const bg = atm ? "bg-brand-soft" : m === "ITM" ? "bg-surface-2" : "bg-surface";
    const type = side === "call" ? "CE" : "PE";
    const value = col.format(cellValue(row, side, col.key));
    if (col.key === "ltp") {
      return (
        <td key={`${side}-ltp`} className={cn(TD, R, "cursor-pointer font-medium", bg, selected && "bg-brand-soft ring-1 ring-inset ring-brand")} onClick={() => onPick(row.strike, type)}>
          {value}
          {m === "ITM" && !atm && <span className="sr-only"> in the money</span>}
        </td>
      );
    }
    const tone = col.key === "oichg" ? (cellValue(row, side, "oichg") ?? 0) : 0;
    return (
      <td key={`${side}-${col.key}`} className={cn(TD, R, "text-muted", bg, tone > 0 && "text-pos", tone < 0 && "text-neg")}>
        {value}
      </td>
    );
  };
  const edge = atm ? "bg-brand-soft" : "bg-surface";
  return (
    <tr>
      <td className={cn(TD, "w-px pr-0", atm ? "bg-brand-soft" : callBg(row, spot))}>
        <Checkbox checked={ce} onChange={() => onPick(row.strike, "CE")} aria-label={`Select ${fmtInt(row.strike)} CE`} />
      </td>
      {callCols.map((c) => cell("call", c))}
      <td className={cn(TD, R, "sticky left-0 z-1 font-semibold shadow-[1px_0_0_var(--line)]", edge)}>
        {fmtInt(row.strike)}
        {atm && <Badge tone="brand" className="ml-1.5 align-middle">ATM</Badge>}
      </td>
      {putCols.map((c) => cell("put", c))}
      <td className={cn(TD, "w-px pr-0", atm ? "bg-brand-soft" : putBg(row, spot))}>
        <Checkbox checked={pe} onChange={() => onPick(row.strike, "PE")} aria-label={`Select ${fmtInt(row.strike)} PE`} />
      </td>
      <td className={cn(TD, R, edge)}>{fmt(row.combined_value)}</td>
      <td className={cn(TD, edge)}>
        <button type="button" onClick={() => onTrade(row.strike)} aria-label={`Trade ${fmtInt(row.strike)}`} className="grid size-8 place-items-center rounded-control text-muted hover:bg-surface-2 hover:text-brand-fg">
          <Zap aria-hidden="true" className="size-4" />
        </button>
      </td>
    </tr>
  );
});
const callBg = (row: StrikeRow, spot: number | null) => (moneyness(row.strike, spot, "call", false) === "ITM" ? "bg-surface-2" : "bg-surface");
const putBg = (row: StrikeRow, spot: number | null) => (moneyness(row.strike, spot, "put", false) === "ITM" ? "bg-surface-2" : "bg-surface");

const WINDOWS = [
  { value: "3", label: "±3", title: "3 strikes either side of the money" },
  { value: "6", label: "±6", title: "6 strikes either side of the money" },
  { value: "all", label: "All" },
] as const;

export function OptionChainCard({ options, atm, loading }: { options: OptionsTopic | undefined; atm: number | null; loading: boolean }) {
  const sel = useSelection();
  const { openOrder } = useUI();
  const { state: watch, update: updateWatch } = useWatchlists();
  const [win, setWin] = useState<Window>(6);
  const [query, setQuery] = useState("");
  const [sort, setSort] = useState<Sort>(DEFAULT_SORT);
  const [visible, setVisible] = useState<ColumnKey[]>(loadColumns);

  const spot = options?.underlying_ltp ?? null;
  const rows = useMemo(() => sortRows(filterRows(options?.strikes ?? [], { window: win, atm, query }), sort), [options?.strikes, win, atm, query, sort]);
  const total = options?.strikes.length ?? 0;
  const callCols = useMemo(() => orderedColumns(visible, "call"), [visible]);
  const putCols = useMemo(() => orderedColumns(visible, "put"), [visible]);

  const expiry = options?.expiry ?? null;
  const underlying = options?.underlying ?? null;
  const toggle = sel.toggle;
  const onPick = useCallback((strike: number, optionType: "CE" | "PE") => toggle({ strike, optionType, expiry, underlying }), [toggle, expiry, underlying]);
  const onTrade = useCallback((strike: number) => openOrder({ strike, optionType: "CE" }), [openOrder]);
  const count = sel.selection.size;

  function watchSelected() {
    updateWatch((w) => {
      let next = w;
      for (const e of sel.selection.values()) {
        const row = options?.strikes.find((r) => r.strike === e.strike);
        const price = row ? (e.optionType === "CE" ? row.call_ltp : row.put_ltp) : null;
        next = addItem(next, w.active, { kind: "option", underlying: e.underlying ?? underlying ?? "", strike: e.strike, optionType: e.optionType, expiry: e.expiry ?? expiry, addedPrice: price });
      }
      return next;
    });
  }

  function toggleColumn(key: ColumnKey) {
    if (key === "ltp") return; // the price is what you select, so it always stays
    setVisible((v) => {
      const next = v.includes(key) ? v.filter((k) => k !== key) : [...v, key];
      saveColumns(next);
      return next;
    });
  }

  const header = (key: string, label: string, title: string, extra?: string) => {
    const active = sort.key === key;
    return (
      <th key={key} scope="col" aria-sort={active ? (sort.dir === "asc" ? "ascending" : "descending") : "none"} className={cn(TH, R, extra)}>
        <button type="button" onClick={() => setSort((s) => nextSort(s, key))} title={`Sort by ${title}`} className={cn("inline-flex min-h-6 items-center gap-0.5 uppercase tracking-wide hover:text-fg", active && "text-fg")}>
          {label}
          {active && (sort.dir === "asc" ? <ArrowUp aria-hidden="true" className="size-3" /> : <ArrowDown aria-hidden="true" className="size-3" />)}
        </button>
      </th>
    );
  };

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
            <Button size="sm" onClick={watchSelected} title={`Add the selected contracts to "${watch.lists.find((l) => l.id === watch.active)?.name}"`}>Watch</Button>
            <Button size="sm" onClick={sel.clear}>Clear</Button>
            <Link to="/trading" className="inline-flex min-h-8 items-center gap-1.5 rounded-control bg-brand px-3 text-sm font-medium text-on-brand hover:brightness-110">
              Trade {count} selected <ArrowRight aria-hidden="true" className="size-4" />
            </Link>
          </>
        )
      }
    >
      {loading ? (
        <div className="px-4 pb-4 sm:px-5"><div aria-busy="true" role="status" className="h-48 animate-pulse rounded-control bg-surface-2"><span className="sr-only">Loading option chain</span></div></div>
      ) : total === 0 ? (
        <EmptyState title="No option chain yet">The chain appears once the market feed delivers it.</EmptyState>
      ) : (
        <div className="px-2 pb-3 sm:px-3">
          <div className="mb-2 flex flex-wrap items-center gap-2 px-2">
            <Segmented label="Strikes shown around the money" size="sm" options={WINDOWS} value={String(win)} onChange={(v) => setWin(v === "all" ? "all" : (Number(v) as 3 | 6))} />
            <Input aria-label="Find a strike" placeholder="Find strike" inputMode="numeric" value={query} onChange={(e) => setQuery(e.target.value)} className="min-h-8 w-32 py-1" />
            <details className="relative">
              <summary className="inline-flex min-h-8 cursor-pointer list-none items-center gap-1.5 rounded-control border border-control bg-surface px-3 text-sm font-medium hover:bg-surface-2">
                <Columns3 aria-hidden="true" className="size-4" />
                Columns
              </summary>
              <div className="absolute z-30 mt-1 grid w-56 gap-1 rounded-card border border-line bg-surface p-3 shadow-pop">
                {COLUMNS.map((c) => (
                  <label key={c.key} className="flex min-h-8 items-center gap-2 text-sm">
                    <Checkbox checked={visible.includes(c.key)} disabled={c.key === "ltp"} onChange={() => toggleColumn(c.key)} />
                    <span>{c.title}</span>
                  </label>
                ))}
              </div>
            </details>
            {options?.greeks_source && <Badge tone="neutral" className="ml-auto" ><span title={GREEKS_NOTE[options.greeks_source]}>Greeks: {options.greeks_source.replace("+", " + ")}</span></Badge>}
          </div>

          {rows.length === 0 ? (
            <EmptyState title="No matching strike">No strike contains “{query}”.</EmptyState>
          ) : (
            <TableWrap label="Option chain" className="max-h-128">
              <table className={TABLE}>
                <caption className="sr-only">Option chain by strike. Calls on the left, puts on the right; shaded cells are in the money. Column headers sort.</caption>
                <thead>
                  <tr>
                    <th className={TH}><span className="sr-only">Select call</span></th>
                    {callCols.map((c) => header(`call:${c.key}`, `CE ${c.label}`, `call ${c.title.toLowerCase()}`))}
                    {header("strike", "Strike", "strike", "sticky left-0 z-20")}
                    {putCols.map((c) => header(`put:${c.key}`, `PE ${c.label}`, `put ${c.title.toLowerCase()}`))}
                    <th className={TH}><span className="sr-only">Select put</span></th>
                    <th className={cn(TH, R)}>CE+PE</th>
                    <th className={TH}><span className="sr-only">Trade</span></th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((r) => (
                    <ChainRow key={r.strike} row={r} spot={spot} callCols={callCols} putCols={putCols} ce={sel.selection.has(selectionKey(r.strike, "CE"))} pe={sel.selection.has(selectionKey(r.strike, "PE"))} onPick={onPick} onTrade={onTrade} />
                  ))}
                </tbody>
              </table>
            </TableWrap>
          )}
          <p className="mt-2 px-2 text-xs text-muted">
            Showing {rows.length} of {total} strikes. Shaded cells are in the money. IV is annualised, theta is per day, vega is per volatility point.
          </p>
        </div>
      )}
    </Card>
  );
}
