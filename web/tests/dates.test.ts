import { describe, expect, it } from "vitest";
import { fmtDateDMY, navigableDates, nextAvailable, previousAvailable, snapToAvailable, todayIST } from "../src/lib/dates";

const DATES = ["2026-10-15", "2026-09-17", "2026-09-11"]; // server order: newest first
const TODAY = "2026-10-16";

describe("trading-date navigation", () => {
  it("adds today, sorts ascending and removes duplicates", () => {
    expect(navigableDates(DATES, TODAY)).toEqual(["2026-09-11", "2026-09-17", "2026-10-15", "2026-10-16"]);
    expect(navigableDates([TODAY, ...DATES], TODAY)).toHaveLength(4);
  });

  it("previous skips days without data instead of stepping one calendar day", () => {
    expect(previousAvailable(DATES, TODAY, "2026-10-15")).toBe("2026-09-17");
    expect(previousAvailable(DATES, TODAY, "2026-09-17")).toBe("2026-09-11");
    expect(previousAvailable(DATES, TODAY, "2026-09-11")).toBeNull();
  });

  it("next skips days without data and ends at today", () => {
    expect(nextAvailable(DATES, TODAY, "2026-09-11")).toBe("2026-09-17");
    expect(nextAvailable(DATES, TODAY, "2026-10-15")).toBe(TODAY);
    expect(nextAvailable(DATES, TODAY, TODAY)).toBeNull();
  });

  it("works from a date that itself has no data", () => {
    expect(previousAvailable(DATES, TODAY, "2026-10-01")).toBe("2026-09-17");
    expect(nextAvailable(DATES, TODAY, "2026-10-01")).toBe("2026-10-15");
  });

  it("snaps a picked date to the nearest earlier day with data, then later, then today", () => {
    expect(snapToAvailable(DATES, TODAY, "2026-09-17")).toBe("2026-09-17");
    expect(snapToAvailable(DATES, TODAY, "2026-10-01")).toBe("2026-09-17");
    expect(snapToAvailable(DATES, TODAY, "2020-01-01")).toBe("2026-09-11");
    expect(snapToAvailable([], TODAY, "2026-01-01")).toBe(TODAY);
  });

  it("formats dates by reordering, never through a Date object", () => {
    expect(fmtDateDMY("2026-10-16")).toBe("16-10-2026");
    expect(fmtDateDMY(null)).toBe("—");
    expect(fmtDateDMY("garbage")).toBe("—");
  });

  it("gives India's calendar date even when the viewer's UTC date differs", () => {
    expect(todayIST(new Date("2026-10-16T20:00:00Z"))).toBe("2026-10-17"); // 01:30 IST next day
    expect(todayIST(new Date("2026-10-16T05:00:00Z"))).toBe("2026-10-16");
  });
});
