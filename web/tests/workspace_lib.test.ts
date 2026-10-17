import { describe, expect, it } from "vitest";
import { GO_TARGETS, interpret, type KeyInfo } from "../src/lib/shortcuts";
import {
  MAX_ITEMS, MAX_LISTS, addItem, createList, defaultState, deleteList, itemKey, parseState, quoteFor, removeItem, renameList, setActive, type WatchItem,
} from "../src/lib/watchlist";
import { DEFAULT_WORKSPACE, gridClass, parseWorkspace, paneCount } from "../src/lib/workspace";
import type { IndexTopic, OptionsTopic } from "../src/api/types";

const opt = (strike: number, optionType: "CE" | "PE" = "CE", expiry = "22-Oct-2026"): Omit<WatchItem, "id"> => ({ kind: "option", underlying: "NIFTY", strike, optionType, expiry, addedPrice: 100 });

describe("watchlist state", () => {
  it("starts with one empty list", () => {
    const s = defaultState();
    expect(s.lists).toHaveLength(1);
    expect(s.active).toBe(s.lists[0]!.id);
  });
  it("adds items once: the same contract is not listed twice, a different expiry is a different contract", () => {
    let s = defaultState();
    const id = s.active;
    s = addItem(s, id, opt(24850));
    s = addItem(s, id, opt(24850));
    s = addItem(s, id, opt(24850, "PE"));
    s = addItem(s, id, opt(24850, "CE", "29-Oct-2026"));
    expect(s.lists[0]!.items).toHaveLength(3);
  });
  it("caps a list", () => {
    let s = defaultState();
    for (let k = 0; k < MAX_ITEMS + 5; k++) s = addItem(s, s.active, opt(20000 + k * 50));
    expect(s.lists[0]!.items).toHaveLength(MAX_ITEMS);
  });
  it("removes an item by id and leaves the rest", () => {
    let s = defaultState();
    s = addItem(s, s.active, opt(24800));
    s = addItem(s, s.active, opt(24900));
    const first = s.lists[0]!.items[0]!;
    s = removeItem(s, s.active, first.id);
    expect(s.lists[0]!.items.map((i) => i.strike)).toEqual([24900]);
  });
  it("creates, renames and selects lists, capped, and always keeps one", () => {
    let s = createList(defaultState(), "  Weekly   straddles ");
    expect(s.lists.map((l) => l.name)).toEqual(["Watchlist 1", "Weekly straddles"]);
    expect(s.active).toBe(s.lists[1]!.id);
    s = renameList(s, s.active, "   ");
    expect(s.lists[1]!.name).toBe("Weekly straddles"); // a blank name is ignored
    s = renameList(s, s.active, "x".repeat(100));
    expect(s.lists[1]!.name).toHaveLength(40);
    s = setActive(s, s.lists[0]!.id);
    s = deleteList(s, s.lists[0]!.id);
    expect(s.lists).toHaveLength(1);
    expect(s.active).toBe(s.lists[0]!.id);
    expect(deleteList(s, s.active).lists).toHaveLength(1);
    expect(setActive(s, "nope")).toBe(s);
    let many = defaultState();
    for (let i = 0; i < MAX_LISTS + 3; i++) many = createList(many, "");
    expect(many.lists).toHaveLength(MAX_LISTS);
  });
});

describe("restoring from storage", () => {
  it("round-trips a valid state", () => {
    let s = defaultState();
    s = addItem(s, s.active, opt(24850));
    expect(parseState(JSON.parse(JSON.stringify(s)))).toEqual(s);
  });
  it("falls back to a fresh state for junk", () => {
    for (const junk of [null, 5, "x", {}, { lists: "no" }, { lists: [] }, { lists: [null, 3] }]) {
      expect(parseState(junk).lists).toHaveLength(1);
    }
  });
  it("drops bad items but keeps good ones, and repairs a dangling active id", () => {
    const s = parseState({
      active: "gone",
      lists: [{ id: "a", name: "A", items: [
        { id: "1", kind: "option", underlying: "NIFTY", strike: 24850, optionType: "CE", expiry: "22-Oct-2026", addedPrice: 5 },
        { id: "2", kind: "option", underlying: "NIFTY", strike: "x", optionType: "CE" },
        { id: "3", kind: "option", underlying: "NIFTY", strike: 1, optionType: "XX" },
        { id: "4", kind: "weird", underlying: "NIFTY" },
        { id: "5", kind: "index", underlying: "<script>" },
        { id: "6", kind: "option", underlying: "NIFTY", strike: 24850, optionType: "CE", expiry: "22-Oct-2026" },
        { id: "7", kind: "index", underlying: "NIFTY", addedPrice: "nope" },
      ] }],
    });
    expect(s.active).toBe("a");
    expect(s.lists[0]!.items.map((i) => i.id)).toEqual(["1", "7"]); // 6 duplicates 1
    expect(s.lists[0]!.items[1]!.addedPrice).toBeNull();
  });
  it("identifies contracts by underlying, expiry, strike and type", () => {
    expect(itemKey({ kind: "option", underlying: "NIFTY", strike: 1, optionType: "CE", expiry: "a" })).not.toBe(itemKey({ kind: "option", underlying: "NIFTY", strike: 1, optionType: "CE", expiry: "b" }));
  });
});

describe("quotes", () => {
  const options = { underlying: "NIFTY", expiry: "22-Oct-2026", strikes: [{ strike: 24850, call_ltp: 150, put_ltp: null }] } as unknown as OptionsTopic;
  const index = { underlying: "NIFTY", price: 24851 } as IndexTopic;
  const item = (o: Partial<WatchItem>): WatchItem => ({ id: "i", ...opt(24850), ...o }) as WatchItem;
  it("prices a live contract", () => {
    expect(quoteFor(item({}), index, options)).toEqual({ price: 150, state: "live" });
    expect(quoteFor({ id: "x", kind: "index", underlying: "NIFTY", addedPrice: null }, index, options)).toEqual({ price: 24851, state: "live" });
  });
  it("never shows another expiry's or another underlying's price", () => {
    expect(quoteFor(item({ expiry: "29-Oct-2026" }), index, options)).toEqual({ price: null, state: "other-expiry" });
    expect(quoteFor(item({ underlying: "BANKNIFTY" }), index, options)).toEqual({ price: null, state: "other-underlying" });
    expect(quoteFor({ id: "x", kind: "index", underlying: "BANKNIFTY", addedPrice: null }, index, options).state).toBe("other-underlying");
  });
  it("reports a missing price rather than zero", () => {
    expect(quoteFor(item({ optionType: "PE" }), index, options)).toEqual({ price: null, state: "no-price" });
    expect(quoteFor(item({ strike: 99999 }), index, options).state).toBe("no-price");
    expect(quoteFor(item({}), undefined, undefined).state).toBe("other-underlying");
  });
});

describe("workspace layout", () => {
  it("restores a valid layout, and fills in or discards what is invalid", () => {
    expect(parseWorkspace({ layout: "4", panes: ["strangle", "price", "straddle", "price"] })).toEqual({ layout: "4", panes: ["strangle", "price", "straddle", "price"] });
    expect(parseWorkspace(null)).toEqual(DEFAULT_WORKSPACE);
    expect(parseWorkspace({ layout: "9", panes: ["nope", "price"] })).toEqual({ layout: DEFAULT_WORKSPACE.layout, panes: ["price", "straddle", "strangle", "price"] });
    expect(parseWorkspace({ layout: "1", panes: ["price"] }).panes).toHaveLength(4);
  });
  it("counts panes and chooses a grid that stacks on a phone", () => {
    expect(["1", "2", "4"].map((l) => paneCount(l as "1"))).toEqual([1, 2, 4]);
    expect(gridClass("1")).toBe("grid-cols-1");
    expect(gridClass("4")).toContain("lg:grid-cols-2");
    expect(gridClass("4")).toContain("grid-cols-1");
  });
});

describe("keyboard shortcuts", () => {
  const k = (key: string, o: Partial<KeyInfo> = {}): KeyInfo => ({ key, ctrlKey: false, metaKey: false, altKey: false, editable: false, modalOpen: false, goArmed: false, ...o });
  it("maps the single keys", () => {
    expect(interpret(k("n"))).toEqual({ type: "order" });
    expect(interpret(k("N"))).toEqual({ type: "order" });
    expect(interpret(k("/"))).toEqual({ type: "palette" });
    expect(interpret(k("?"))).toEqual({ type: "help" });
    expect(interpret(k("g"))).toEqual({ type: "arm-go" });
    expect(interpret(k("z"))).toBeNull();
  });
  it("g then a letter navigates; g then anything else does nothing", () => {
    for (const [key, t] of Object.entries(GO_TARGETS)) expect(interpret(k(key, { goArmed: true }))).toEqual({ type: "go", to: t.to });
    expect(interpret(k("x", { goArmed: true }))).toBeNull();
    expect(interpret(k("n", { goArmed: true }))).toBeNull(); // not treated as 'new order'
  });
  it("never fires while typing, in a modal, or with a modifier (so browser and OS shortcuts keep working)", () => {
    expect(interpret(k("n", { editable: true }))).toBeNull();
    expect(interpret(k("n", { modalOpen: true }))).toBeNull();
    expect(interpret(k("n", { ctrlKey: true }))).toBeNull();
    expect(interpret(k("n", { metaKey: true }))).toBeNull();
    expect(interpret(k("n", { altKey: true }))).toBeNull();
  });
});
