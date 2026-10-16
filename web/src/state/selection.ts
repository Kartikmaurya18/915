/**
 * The set of option contracts the user has picked for paper trading. One mechanism for everything:
 * one contract is a single trade, several is a multi-strike trade, and the straddle chart follows it.
 * Kept as pure functions so the rules are testable without React.
 */

export type OptionType = "CE" | "PE";
export type Side = "BUY" | "SELL";

export interface Selected {
  key: string;
  strike: number;
  optionType: OptionType;
  side: Side;
  quantity: number;
  /** Contract identity captured from the chain it was picked in; the server refuses it if the chain moved on. */
  expiry: string | null;
  underlying: string | null;
  /** Idempotency key for this exact order. Changing side or quantity issues a new one. */
  nonce: string;
}

export type Selection = ReadonlyMap<string, Selected>;

export const selectionKey = (strike: number, optionType: OptionType): string => `${strike}_${optionType}`;

const newNonce = (): string => crypto.randomUUID();

export function toggle(
  selection: Selection,
  pick: { strike: number; optionType: OptionType; expiry: string | null; underlying: string | null },
  defaultQuantity: number,
): Selection {
  const key = selectionKey(pick.strike, pick.optionType);
  const next = new Map(selection);
  if (next.has(key)) {
    next.delete(key);
  } else {
    next.set(key, { key, ...pick, side: "BUY", quantity: defaultQuantity, nonce: newNonce() });
  }
  return next;
}

export function update(selection: Selection, key: string, change: Partial<Pick<Selected, "side" | "quantity">>): Selection {
  const current = selection.get(key);
  if (!current) return selection;
  const quantity = change.quantity !== undefined && Number.isInteger(change.quantity) && change.quantity > 0 ? change.quantity : current.quantity;
  const side = change.side ?? current.side;
  if (quantity === current.quantity && side === current.side) return selection;
  const next = new Map(selection);
  next.set(key, { ...current, side, quantity, nonce: newNonce() }); // a different order must not reuse the old key
  return next;
}

export function remove(selection: Selection, keys: Iterable<string>): Selection {
  const next = new Map(selection);
  for (const k of keys) next.delete(k);
  return next;
}

/** Selected contracts in a stable display order: by strike, then CE before PE. */
export function sorted(selection: Selection): Selected[] {
  return [...selection.values()].sort((a, b) => a.strike - b.strike || a.optionType.localeCompare(b.optionType));
}

export function distinctStrikes(selection: Selection): number[] {
  return [...new Set([...selection.values()].map((s) => s.strike))].sort((a, b) => a - b);
}

/**
 * Which strike the straddle chart shows:
 *  - nothing selected: follow the at-the-money strike;
 *  - one strike selected: that strike;
 *  - several: the user's pick if it is still selected, else the lowest.
 */
export function focusStrike(selection: Selection, atm: number | null, picked: number | null): number | null {
  const strikes = distinctStrikes(selection);
  if (strikes.length === 0) return atm;
  if (strikes.length === 1) return strikes[0]!;
  return picked !== null && strikes.includes(picked) ? picked : strikes[0]!;
}
