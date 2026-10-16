import { describe, expect, it } from "vitest";
import { distinctStrikes, focusStrike, remove, selectionKey, sorted, toggle, update, type Selection } from "../src/state/selection";

const pick = (strike: number, optionType: "CE" | "PE") => ({ strike, optionType, expiry: "22-Oct-2026", underlying: "NIFTY" });
const empty: Selection = new Map();

describe("selection", () => {
  it("toggles a contract on and off", () => {
    const on = toggle(empty, pick(24800, "CE"), 50);
    expect(on.size).toBe(1);
    expect(on.get(selectionKey(24800, "CE"))).toMatchObject({ side: "BUY", quantity: 50, expiry: "22-Oct-2026", underlying: "NIFTY" });
    expect(toggle(on, pick(24800, "CE"), 50).size).toBe(0);
  });

  it("treats CE and PE of one strike as separate contracts", () => {
    const s = toggle(toggle(empty, pick(24800, "CE"), 50), pick(24800, "PE"), 50);
    expect(s.size).toBe(2);
    expect(distinctStrikes(s)).toEqual([24800]);
  });

  it("never mutates the previous selection", () => {
    const a = toggle(empty, pick(24800, "CE"), 50);
    toggle(a, pick(24850, "PE"), 50);
    update(a, selectionKey(24800, "CE"), { quantity: 5 });
    remove(a, [selectionKey(24800, "CE")]);
    expect(a.size).toBe(1);
    expect(a.get(selectionKey(24800, "CE"))?.quantity).toBe(50);
  });

  it("changing side or quantity issues a new idempotency key; a no-op change keeps it", () => {
    const base = toggle(empty, pick(24800, "CE"), 50);
    const key = selectionKey(24800, "CE");
    const nonce = base.get(key)!.nonce;
    expect(update(base, key, { quantity: 50 })).toBe(base);
    expect(update(base, key, { side: "BUY" })).toBe(base);
    expect(update(base, key, { quantity: 75 }).get(key)!.nonce).not.toBe(nonce);
    expect(update(base, key, { side: "SELL" }).get(key)!.nonce).not.toBe(nonce);
  });

  it("ignores invalid quantities", () => {
    const base = toggle(empty, pick(24800, "CE"), 50);
    const key = selectionKey(24800, "CE");
    for (const bad of [0, -5, 2.5, Number.NaN]) expect(update(base, key, { quantity: bad })).toBe(base);
  });

  it("ignores updates for a contract that is not selected", () => {
    expect(update(empty, "nope", { side: "SELL" })).toBe(empty);
  });

  it("removes only the named contracts", () => {
    const s = toggle(toggle(empty, pick(24800, "CE"), 50), pick(24850, "PE"), 50);
    const after = remove(s, [selectionKey(24800, "CE")]);
    expect([...after.keys()]).toEqual([selectionKey(24850, "PE")]);
  });

  it("sorts by strike, then CE before PE", () => {
    let s: Selection = empty;
    for (const [k, t] of [[24850, "PE"], [24800, "PE"], [24800, "CE"]] as const) s = toggle(s, pick(k, t), 50);
    expect(sorted(s).map((x) => `${x.strike}${x.optionType}`)).toEqual(["24800CE", "24800PE", "24850PE"]);
  });
});

describe("straddle focus", () => {
  const sel = (...strikes: number[]) => strikes.reduce<Selection>((s, k) => toggle(s, pick(k, "CE"), 50), empty);

  it("follows ATM when nothing is selected", () => {
    expect(focusStrike(empty, 24850, null)).toBe(24850);
    expect(focusStrike(empty, null, null)).toBeNull();
  });

  it("tracks the one selected strike regardless of ATM", () => {
    expect(focusStrike(sel(24700), 24850, null)).toBe(24700);
  });

  it("with several selected uses the picked one if still selected, else the lowest", () => {
    expect(focusStrike(sel(24900, 24700, 24800), 24850, 24800)).toBe(24800);
    expect(focusStrike(sel(24900, 24700, 24800), 24850, 99999)).toBe(24700);
    expect(focusStrike(sel(24900, 24700), 24850, null)).toBe(24700);
  });
});
