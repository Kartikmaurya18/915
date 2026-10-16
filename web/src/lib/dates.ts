/**
 * Trading-date navigation. Dates are plain "YYYY-MM-DD" strings (Asia/Kolkata calendar days) and are
 * compared as strings: no Date objects, so no timezone reinterpretation can shift a day.
 */

/** Every date that can be shown: the days with persisted data, plus today, ascending and de-duplicated. */
export function navigableDates(dates: readonly string[], today: string): string[] {
  return [...new Set([...dates, today])].sort();
}

/** The nearest navigable date strictly before `iso`, or null. This is what skips empty days. */
export function previousAvailable(dates: readonly string[], today: string, iso: string): string | null {
  let prev: string | null = null;
  for (const d of navigableDates(dates, today)) {
    if (d < iso) prev = d;
    else break;
  }
  return prev;
}

/** The nearest navigable date strictly after `iso`, or null. */
export function nextAvailable(dates: readonly string[], today: string, iso: string): string | null {
  for (const d of navigableDates(dates, today)) if (d > iso) return d;
  return null;
}

/** A typed or picked date may have no data: prefer the nearest earlier day, then the nearest later, then today. */
export function snapToAvailable(dates: readonly string[], today: string, iso: string): string {
  const all = navigableDates(dates, today);
  if (all.includes(iso)) return iso;
  return previousAvailable(dates, today, iso) ?? nextAvailable(dates, today, iso) ?? today;
}

/** "2026-10-16" -> "16-10-2026" by reordering the string only. */
export function fmtDateDMY(iso: string | null | undefined): string {
  if (!iso) return "—";
  const [y, m, d] = iso.split("-");
  return y && m && d ? `${d}-${m}-${y}` : "—";
}

/** Today's calendar date in India, whatever the viewer's own time zone. */
export function todayIST(now: Date = new Date()): string {
  return now.toLocaleDateString("en-CA", { timeZone: "Asia/Kolkata" });
}
