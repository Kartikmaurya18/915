import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

/**
 * The design tokens live in src/styles.css. This test parses them and enforces WCAG 2.2 AA, so a palette
 * change that breaks contrast fails the build. (docs/DESIGN.md has the resulting table.)
 */
const css = readFileSync(resolve(process.cwd(), "src/styles.css"), "utf-8");

function block(selector: string): Record<string, string> {
  const start = css.indexOf(selector + " {");
  if (start < 0) throw new Error("no block " + selector);
  const body = css.slice(css.indexOf("{", start) + 1, css.indexOf("\n}", start));
  const vars: Record<string, string> = {};
  for (const m of body.matchAll(/--([a-z0-9-]+):\s*(#[0-9a-fA-F]{6})\b/g)) vars[m[1]!] = m[2]!;
  return vars;
}

const themes = { light: block(":root"), dark: block(".dark") };

function luminance(hex: string): number {
  const [r, g, b] = (hex.slice(1).match(/../g) ?? []).map((h) => parseInt(h, 16) / 255).map((v) => (v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4));
  return 0.2126 * r! + 0.7152 * g! + 0.0722 * b!;
}
export function ratio(a: string, b: string): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi! + 0.05) / (lo! + 0.05);
}

// [foreground, background, minimum ratio, what it is]
const TEXT = 4.5;
const UI = 3;
const PAIRS: [string, string, number, string][] = [
  ["fg", "surface", TEXT, "body text on cards"],
  ["fg", "bg", TEXT, "body text on the page"],
  ["muted", "surface", TEXT, "secondary text"],
  ["muted", "bg", TEXT, "secondary text on the page"],
  ["muted", "surface-2", TEXT, "secondary text on subtle fills"],
  ["subtle", "surface", TEXT, "captions"],
  ["subtle", "surface-2", TEXT, "captions on subtle fills"],
  ["brand-fg", "surface", TEXT, "links and brand text"],
  ["brand-fg", "brand-soft", TEXT, "text on brand tints"],
  ["on-brand", "brand", TEXT, "primary button label"],
  ["pos", "surface", TEXT, "gains"],
  ["pos", "pos-soft", TEXT, "gain badges"],
  ["neg", "surface", TEXT, "losses"],
  ["neg", "neg-soft", TEXT, "loss badges"],
  ["warn", "warn-soft", TEXT, "warnings"],
  ["control", "surface", UI, "input and checkbox borders"],
  ["brand", "surface", UI, "brand fills and focus rings"],
  ["ring", "surface", UI, "focus ring"],
  ["ring", "bg", UI, "focus ring on the page"],
];

describe("design tokens meet WCAG 2.2 AA in both themes", () => {
  it("defines the same tokens in both themes", () => {
    expect(Object.keys(themes.dark).sort()).toEqual(Object.keys(themes.light).sort());
    expect(Object.keys(themes.light).length).toBeGreaterThan(15);
  });

  for (const [theme, tokens] of Object.entries(themes)) {
    for (const [fg, bg, min, what] of PAIRS) {
      it(`${theme}: ${what} (${fg} on ${bg}) is at least ${min}:1`, () => {
        expect(tokens[fg], `missing token ${fg}`).toBeTruthy();
        expect(tokens[bg], `missing token ${bg}`).toBeTruthy();
        expect(ratio(tokens[fg]!, tokens[bg]!)).toBeGreaterThanOrEqual(min);
      });
    }
  }

  it("keeps gains and losses distinguishable from the brand colour by more than hue alone", () => {
    // Green and red must differ in luminance as well as hue, for colour-blind users.
    for (const tokens of Object.values(themes)) {
      expect(Math.abs(luminance(tokens["pos"]!) - luminance(tokens["neg"]!))).toBeGreaterThan(0.03);
    }
  });
});
