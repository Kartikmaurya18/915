/** Mobile numbers for the sign-up form. The server accepts only E.164, so the form builds that and shows it back. */

export const COUNTRIES: readonly { code: string; name: string }[] = [
  { code: "91", name: "India" },
  { code: "1", name: "United States / Canada" },
  { code: "44", name: "United Kingdom" },
  { code: "971", name: "United Arab Emirates" },
  { code: "65", name: "Singapore" },
  { code: "61", name: "Australia" },
  { code: "49", name: "Germany" },
  { code: "33", name: "France" },
  { code: "81", name: "Japan" },
  { code: "86", name: "China" },
  { code: "966", name: "Saudi Arabia" },
  { code: "977", name: "Nepal" },
  { code: "94", name: "Sri Lanka" },
  { code: "880", name: "Bangladesh" },
  { code: "92", name: "Pakistan" },
];

export const DEFAULT_COUNTRY = "91";

/**
 * "+<country><number>" from what the person typed. Spaces, dashes and brackets are ignored, and so is one leading
 * trunk zero ("098765 43210" is dialled without it abroad). Returns null while the number cannot be valid.
 */
export function buildE164(countryCode: string, national: string): string | null {
  const digits = national.replace(/[^\d]/g, "").replace(/^0/, "");
  const full = `+${countryCode}${digits}`;
  return /^\+[1-9]\d{7,14}$/.test(full) ? full : null;
}

/** "+91 98765 43210"-style, for confirming back to the person where the code is going. */
export function formatPreview(e164: string, countryCode: string): string {
  const rest = e164.slice(1 + countryCode.length);
  const grouped = rest.length === 10 ? `${rest.slice(0, 5)} ${rest.slice(5)}` : rest.replace(/(\d{3})(?=\d)/g, "$1 ");
  return `+${countryCode} ${grouped}`;
}
