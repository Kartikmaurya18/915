import { describe, expect, it, vi } from "vitest";
import type { PaperPosition } from "../src/api/types";
import { executeEntries, summarise } from "../src/state/execute";
import { toggle, type Selection } from "../src/state/selection";

const pick = (strike: number, optionType: "CE" | "PE") => ({ strike, optionType, expiry: "22-Oct-2026", underlying: "NIFTY" });
const build = (...picks: ReturnType<typeof pick>[]): Selection => picks.reduce<Selection>((s, p) => toggle(s, p, 50), new Map());
const entries = (s: Selection) => [...s.values()];
const position = {} as PaperPosition;

describe("executeEntries", () => {
  it("opens every contract independently and reports all successes", async () => {
    const sel = entries(build(pick(24800, "CE"), pick(24850, "PE")));
    const open = vi.fn(async () => position);
    const result = await executeEntries(sel, open);
    expect(open).toHaveBeenCalledTimes(2);
    expect(result.succeeded).toHaveLength(2);
    expect(result.failed).toHaveLength(0);
    expect(summarise(result)).toEqual({ ok: true, text: "Executed 2 paper positions, each independent." });
  });

  it("one failure does not stop the others, and the failed entry is identified", async () => {
    const sel = entries(build(pick(24800, "CE"), pick(24850, "PE"), pick(24900, "CE")));
    const open = vi.fn(async (e: { strike: number }) => {
      if (e.strike === 24850) throw new Error("No price for PE 24850.");
      return position;
    });
    const result = await executeEntries(sel, open);
    expect(result.succeeded.map((e) => e.strike)).toEqual([24800, 24900]);
    expect(result.failed).toHaveLength(1);
    expect(result.failed[0]!.entry.strike).toBe(24850);
    expect(summarise(result)).toEqual({ ok: false, text: "2 executed, 1 failed (24850 PE: No price for PE 24850.)" });
  });

  it("runs the requests concurrently, not one after another", async () => {
    const sel = entries(build(pick(1, "CE"), pick(2, "CE"), pick(3, "CE")));
    let inFlight = 0;
    let peak = 0;
    await executeEntries(sel, async () => {
      peak = Math.max(peak, ++inFlight);
      await new Promise((r) => setTimeout(r, 10));
      inFlight--;
      return position;
    });
    expect(peak).toBe(3);
  });

  it("gives each order its own idempotency nonce", () => {
    const nonces = entries(build(pick(1, "CE"), pick(1, "PE"), pick(2, "CE"))).map((e) => e.nonce);
    expect(new Set(nonces).size).toBe(3);
  });

  it("handles a non-Error rejection and an empty batch", async () => {
    const [only] = entries(build(pick(24800, "CE")));
    const result = await executeEntries([only!], () => Promise.reject("boom"));
    expect(result.failed[0]!.message).toBe("Could not open 24800 CE");
    expect(await executeEntries([], vi.fn())).toEqual({ succeeded: [], failed: [] });
    expect(summarise({ succeeded: [only!], failed: [] }).text).toBe("Executed 1 paper position, each independent.");
  });
});
