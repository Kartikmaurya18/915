/** Number and time formatting. Market times are shown in IST regardless of the viewer's locale. */

const MISSING = "—";

export function fmt(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || Number.isNaN(value)) return MISSING;
  return value.toLocaleString("en-IN", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

export function fmtInt(value: number | null | undefined): string {
  return fmt(value, 0);
}

export function fmtSigned(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || Number.isNaN(value)) return MISSING;
  return (value > 0 ? "+" : "") + fmt(value, digits);
}

/** Compact open-interest / volume style numbers: 1.2L, 3.4Cr as Indian traders read them. */
export function fmtCompact(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return MISSING;
  const abs = Math.abs(value);
  if (abs >= 1e7) return (value / 1e7).toFixed(2) + "Cr";
  if (abs >= 1e5) return (value / 1e5).toFixed(2) + "L";
  if (abs >= 1e3) return (value / 1e3).toFixed(1) + "K";
  return String(Math.round(value));
}

export function pnlClass(value: number | null | undefined): string {
  if (!value) return "";
  return value > 0 ? "pos" : "neg";
}

const istTime = new Intl.DateTimeFormat("en-IN", {
  timeZone: "Asia/Kolkata",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false,
});
const istDateTime = new Intl.DateTimeFormat("en-IN", {
  timeZone: "Asia/Kolkata",
  day: "2-digit",
  month: "short",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
});

export function fmtTime(iso: string | number | null | undefined): string {
  if (iso === null || iso === undefined) return MISSING;
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? MISSING : istTime.format(d);
}

export function fmtDateTime(iso: string | number | null | undefined): string {
  if (iso === null || iso === undefined) return MISSING;
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? MISSING : istDateTime.format(d);
}

export function fmtAge(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return MISSING;
  if (ms < 1000) return `${Math.round(ms)} ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)} s`;
  return `${Math.floor(ms / 60_000)} min`;
}
