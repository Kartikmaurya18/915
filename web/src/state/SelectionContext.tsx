import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from "react";
import { remove, toggle, update, type Selection, type Selected } from "./selection";

interface SelectionApi {
  selection: Selection;
  /** Quantity new picks start with (the trading page's default quantity). */
  defaultQuantity: number;
  setDefaultQuantity(q: number): void;
  toggle(pick: { strike: number; optionType: "CE" | "PE"; expiry: string | null; underlying: string | null }): void;
  update(key: string, change: Partial<Pick<Selected, "side" | "quantity">>): void;
  remove(keys: Iterable<string>): void;
  clear(): void;
  /** The strike the user chose on the straddle chart when several are selected. */
  pickedStrike: number | null;
  setPickedStrike(s: number | null): void;
}

const Ctx = createContext<SelectionApi | null>(null);

/** Lives above the router so a selection made on Overview is still there on the Trading page. */
export function SelectionProvider({ children }: { children: ReactNode }) {
  const [selection, setSelection] = useState<Selection>(new Map());
  const [defaultQuantity, setDefaultQuantity] = useState(50);
  const [pickedStrike, setPickedStrike] = useState<number | null>(null);

  const api = useMemo<SelectionApi>(
    () => ({
      selection,
      defaultQuantity,
      setDefaultQuantity,
      toggle: (pick) => setSelection((s) => toggle(s, pick, defaultQuantity)),
      update: (key, change) => setSelection((s) => update(s, key, change)),
      remove: (keys) => setSelection((s) => remove(s, keys)),
      clear: () => setSelection(new Map()),
      pickedStrike,
      setPickedStrike,
    }),
    [selection, defaultQuantity, pickedStrike],
  );
  return <Ctx.Provider value={api}>{children}</Ctx.Provider>;
}

export function useSelection(): SelectionApi {
  const v = useContext(Ctx);
  if (!v) throw new Error("useSelection must be used inside <SelectionProvider>");
  return v;
}

/** A stable callback-friendly check for "is this contract selected". */
export function useIsSelected(): (strike: number, type: "CE" | "PE") => boolean {
  const { selection } = useSelection();
  return useCallback((strike, type) => selection.has(`${strike}_${type}`), [selection]);
}
