import { describe, expect, it } from "vitest";
import { applyPatch, keyOf, PatchError, type Json } from "../src/realtime/patch";
import cases from "./fixtures/patch_cases.json";

interface Case {
  name: string;
  old: Json;
  new: Json;
  patch: Json;
}

// Fixtures are produced by the real Python diff() (tests/fixtures/gen_patch_fixtures.py), so every
// case below checks that this client reconstructs exactly what the server meant.
describe("applyPatch against patches produced by the Python server", () => {
  const all = cases as unknown as Case[];
  it("has real and edge cases", () => {
    expect(all.filter((c) => c.name.startsWith("real:")).length).toBeGreaterThan(20);
    expect(all.filter((c) => c.name.startsWith("edge:")).length).toBeGreaterThan(15);
  });
  for (const c of all) {
    it(c.name, () => {
      if (c.patch === null) {
        expect(c.old).toEqual(c.new); // the server sends nothing when there is no change
      } else {
        expect(applyPatch(c.old, c.patch)).toEqual(c.new);
      }
    });
  }
});

describe("applyPatch", () => {
  it("does not mutate its input", () => {
    const base = { a: { b: 1 }, rows: [{ strike: 1, v: 1 }] };
    const copy = structuredClone(base);
    applyPatch(base, ["d", { a: ["d", { b: 2 }, []], rows: ["k", "strike", { "n:1.0": ["d", { v: 9 }, []] }, [], null] }, []]);
    expect(base).toEqual(copy);
  });

  it("treats a bare scalar as a replacement and ['=', x] as replacement with x", () => {
    expect(applyPatch({ a: 1 }, ["d", { a: 5 }, []])).toEqual({ a: 5 });
    expect(applyPatch({ a: 1 }, ["d", { a: ["=", null] }, []])).toEqual({ a: null });
    expect(applyPatch(undefined, ["=", [1, 2]])).toEqual([1, 2]);
  });

  it("rejects an unknown node so the client can resync", () => {
    expect(() => applyPatch({}, ["zzz"])).toThrow(PatchError);
  });

  it("rejects an order that names a row it does not have", () => {
    expect(() => applyPatch([{ strike: 1 }], ["k", "strike", {}, [], [1, 2]])).toThrow(PatchError);
  });
});

describe("keyOf matches the Python key encoding", () => {
  it("encodes integers and floats alike, tagged apart from strings", () => {
    expect(keyOf(24850)).toBe("n:24850.0");
    expect(keyOf(24850.0)).toBe("n:24850.0");
    expect(keyOf(100.5)).toBe("n:100.5");
    expect(keyOf("24850")).toBe("s:24850");
  });
});
