/**
 * Watchlists: named lists of instruments to keep an eye on. Pure state transitions plus a validating
 * parser, so what comes back from localStorage (which anyone can edit) can never crash the page.
 */
import type { IndexTopic, OptionsTopic } from "../api/types";

export type WatchKind = "index" | "option";

export interface WatchItem {
  id: string;
  kind: WatchKind;
  underlying: string;
  strike?: number;
  optionType?: "CE" | "PE";
  /** The expiry the contract belongs to; an option watched in one expiry is not the same contract in the next. */
  expiry?: string | null;
  /** Price when it was added, so the list can show the move since. Null if there was none. */
  addedPrice: number | null;
}

export interface WatchList {
  id: string;
  name: string;
  items: WatchItem[];
}

export interface WatchState {
  lists: WatchList[];
  active: string;
}

export const MAX_LISTS = 10;
export const MAX_ITEMS = 50;
export const MAX_NAME = 40;

const newId = (): string => crypto.randomUUID();

export function defaultState(): WatchState {
  const id = newId();
  return { lists: [{ id, name: "Watchlist 1", items: [] }], active: id };
}

export const itemKey = (i: Pick<WatchItem, "kind" | "underlying" | "strike" | "optionType" | "expiry">): string =>
  i.kind === "index" ? `index|${i.underlying}` : `option|${i.underlying}|${i.expiry ?? ""}|${i.strike}|${i.optionType}`;

export const itemLabel = (i: WatchItem): string => (i.kind === "index" ? i.underlying : `${i.underlying} ${i.strike} ${i.optionType}`);

const mapList = (s: WatchState, id: string, fn: (l: WatchList) => WatchList): WatchState => ({ ...s, lists: s.lists.map((l) => (l.id === id ? fn(l) : l)) });

export function addItem(s: WatchState, listId: string, item: Omit<WatchItem, "id">): WatchState {
  return mapList(s, listId, (l) => {
    if (l.items.length >= MAX_ITEMS || l.items.some((x) => itemKey(x) === itemKey(item))) return l;
    return { ...l, items: [...l.items, { ...item, id: newId() }] };
  });
}

export const removeItem = (s: WatchState, listId: string, itemId: string): WatchState => mapList(s, listId, (l) => ({ ...l, items: l.items.filter((x) => x.id !== itemId) }));

export function createList(s: WatchState, name: string): WatchState {
  if (s.lists.length >= MAX_LISTS) return s;
  const id = newId();
  return { lists: [...s.lists, { id, name: cleanName(name) || `Watchlist ${s.lists.length + 1}`, items: [] }], active: id };
}

export const renameList = (s: WatchState, id: string, name: string): WatchState => (cleanName(name) ? mapList(s, id, (l) => ({ ...l, name: cleanName(name) })) : s);

/** The last list cannot be deleted: there is always somewhere to add to. */
export function deleteList(s: WatchState, id: string): WatchState {
  if (s.lists.length <= 1) return s;
  const lists = s.lists.filter((l) => l.id !== id);
  return { lists, active: s.active === id ? lists[0]!.id : s.active };
}

export const setActive = (s: WatchState, id: string): WatchState => (s.lists.some((l) => l.id === id) ? { ...s, active: id } : s);

const cleanName = (n: string): string => n.replace(/\s+/g, " ").trim().slice(0, MAX_NAME);

const isNum = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);

/** Anything unusable falls back to an empty watchlist; individual bad items are dropped, good ones kept. */
export function parseState(raw: unknown): WatchState {
  if (!raw || typeof raw !== "object" || !Array.isArray((raw as WatchState).lists)) return defaultState();
  const lists: WatchList[] = [];
  for (const l of (raw as WatchState).lists.slice(0, MAX_LISTS)) {
    if (!l || typeof l !== "object" || typeof l.id !== "string" || typeof l.name !== "string" || !Array.isArray(l.items)) continue;
    const items: WatchItem[] = [];
    const seen = new Set<string>();
    for (const i of l.items) {
      if (!i || typeof i !== "object" || typeof i.id !== "string" || typeof i.underlying !== "string" || !/^[A-Z0-9]{1,16}$/.test(i.underlying)) continue;
      if (i.kind === "option" && !(isNum(i.strike) && (i.optionType === "CE" || i.optionType === "PE"))) continue;
      if (i.kind !== "option" && i.kind !== "index") continue;
      const item: WatchItem = {
        id: i.id,
        kind: i.kind,
        underlying: i.underlying,
        ...(i.kind === "option" ? { strike: i.strike, optionType: i.optionType, expiry: typeof i.expiry === "string" ? i.expiry : null } : {}),
        addedPrice: isNum(i.addedPrice) ? i.addedPrice : null,
      };
      const key = itemKey(item);
      if (seen.has(key) || items.length >= MAX_ITEMS) continue;
      seen.add(key);
      items.push(item);
    }
    lists.push({ id: l.id, name: cleanName(l.name) || "Watchlist", items });
  }
  if (lists.length === 0) return defaultState();
  const active = typeof (raw as WatchState).active === "string" && lists.some((l) => l.id === (raw as WatchState).active) ? (raw as WatchState).active : lists[0]!.id;
  return { lists, active };
}

export type Quote = { price: number; state: "live" } | { price: null; state: "other-expiry" | "no-price" | "other-underlying" };

/** The current price of an item from the live topics. Never a stale or neighbouring contract's price. */
export function quoteFor(item: WatchItem, index: IndexTopic | undefined, options: OptionsTopic | undefined): Quote {
  if (item.kind === "index") {
    if (!index || index.underlying !== item.underlying) return { price: null, state: "other-underlying" };
    return index.price === null || index.price === undefined ? { price: null, state: "no-price" } : { price: index.price, state: "live" };
  }
  if (!options || options.underlying !== item.underlying) return { price: null, state: "other-underlying" };
  if (item.expiry && options.expiry !== item.expiry) return { price: null, state: "other-expiry" };
  const row = options.strikes.find((r) => r.strike === item.strike);
  const price = row ? (item.optionType === "CE" ? row.call_ltp : row.put_ltp) : null;
  return price === null || price === undefined ? { price: null, state: "no-price" } : { price, state: "live" };
}

const STORAGE_KEY = "md_watchlists";

export function loadState(): WatchState {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return raw ? parseState(JSON.parse(raw)) : defaultState();
  } catch {
    return defaultState();
  }
}

export function saveState(s: WatchState): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(s));
  } catch {
    /* a preference only: the watchlist keeps working for this tab */
  }
}
