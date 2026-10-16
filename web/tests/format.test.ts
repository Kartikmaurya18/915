import { describe, expect, it } from "vitest";
import { fmt, fmtAge, fmtCompact, fmtSigned, fmtTime, pnlClass } from "../src/lib/format";

describe("format", () => {
  it("shows a dash for missing numbers", () => {
    expect(fmt(null)).toBe("—");
    expect(fmt(undefined)).toBe("—");
    expect(fmt(NaN)).toBe("—");
  });
  it("uses Indian digit grouping", () => {
    expect(fmt(1234567.891)).toBe("12,34,567.89");
  });
  it("signs gains explicitly", () => {
    expect(fmtSigned(5)).toBe("+5.00");
    expect(fmtSigned(-5)).toBe("-5.00");
    expect(fmtSigned(0)).toBe("0.00");
  });
  it("compacts large open-interest numbers", () => {
    expect(fmtCompact(250)).toBe("250");
    expect(fmtCompact(64484)).toBe("64.5K");
    expect(fmtCompact(301305)).toBe("3.01L");
    expect(fmtCompact(12_500_000)).toBe("1.25Cr");
  });
  it("classifies P&L", () => {
    expect(pnlClass(3)).toBe("pos");
    expect(pnlClass(-3)).toBe("neg");
    expect(pnlClass(0)).toBe("");
  });
  it("formats ages", () => {
    expect(fmtAge(null)).toBe("—");
    expect(fmtAge(250)).toBe("250 ms");
    expect(fmtAge(2500)).toBe("2.5 s");
    expect(fmtAge(120_000)).toBe("2 min");
  });
  it("renders market time in IST whatever the viewer's zone", () => {
    expect(fmtTime("2026-10-16T04:00:00Z")).toBe("09:30:00");
    expect(fmtTime("not a date")).toBe("—");
  });
});
