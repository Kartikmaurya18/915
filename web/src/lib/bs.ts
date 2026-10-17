/**
 * Black-Scholes-Merton pricing for the "today" curve of a strategy. The server's calculator
 * (src/option_chain/analytics.py) is the reference for implied volatility and Greeks; the browser only
 * needs to re-price a few legs at other underlying levels, so only the price function lives here.
 */
export type OptionType = "CE" | "PE";

/** Complementary error function, Numerical Recipes erfcc (fractional error below 1.2e-7: far finer than a premium tick). */
function erfc(x: number): number {
  const z = Math.abs(x);
  const t = 1 / (1 + 0.5 * z);
  const r =
    t *
    Math.exp(
      -z * z - 1.26551223 +
        t * (1.00002368 + t * (0.37409196 + t * (0.09678418 + t * (-0.18628806 + t * (0.27886807 + t * (-1.13520398 + t * (1.48851587 + t * (-0.82215223 + t * 0.17087277))))))))
    );
  return x >= 0 ? r : 2 - r;
}

export const normCdf = (x: number): number => 0.5 * erfc(-x / Math.SQRT2);

export const RISK_FREE_RATE = 0.065;
export const DIVIDEND_YIELD = 0.012;

const SECONDS_PER_YEAR = 365 * 24 * 3600;
const MONTHS: Record<string, number> = { jan: 0, feb: 1, mar: 2, apr: 3, may: 4, jun: 5, jul: 6, aug: 7, sep: 8, oct: 9, nov: 10, dec: 11 };

/** Settlement instant (15:30 IST = 10:00 UTC) of an expiry written "28-Oct-2026" or "2026-10-28". */
export function expiryInstant(expiry: string | null | undefined): number | null {
  if (!expiry) return null;
  let y: number, m: number, d: number;
  const named = /^(\d{1,2})-([A-Za-z]{3})-(\d{4})$/.exec(expiry.trim());
  const iso = /^(\d{4})-(\d{2})-(\d{2})$/.exec(expiry.trim());
  if (named) {
    d = Number(named[1]);
    m = MONTHS[named[2]!.toLowerCase()] ?? NaN;
    y = Number(named[3]);
  } else if (iso) {
    y = Number(iso[1]);
    m = Number(iso[2]) - 1;
    d = Number(iso[3]);
  } else return null;
  if (Number.isNaN(m)) return null;
  return Date.UTC(y, m, d, 10, 0, 0);
}

/** Calendar years from `nowMs` to settlement, or null when unknown or already past. */
export function yearsToExpiry(expiry: string | null | undefined, nowMs: number): number | null {
  const end = expiryInstant(expiry);
  if (end === null) return null;
  const seconds = (end - nowMs) / 1000;
  return seconds > 0 ? seconds / SECONDS_PER_YEAR : null;
}

/** Theoretical option price. `vol` is a fraction (0.14 = 14 %). */
export function bsPrice(type: OptionType, spot: number, strike: number, years: number, vol: number, r = RISK_FREE_RATE, q = DIVIDEND_YIELD): number {
  if (years <= 0 || vol <= 0 || spot <= 0) return type === "CE" ? Math.max(spot - strike, 0) : Math.max(strike - spot, 0);
  const rootT = Math.sqrt(years);
  const d1 = (Math.log(spot / strike) + (r - q + 0.5 * vol * vol) * years) / (vol * rootT);
  const d2 = d1 - vol * rootT;
  const discR = Math.exp(-r * years);
  const discQ = Math.exp(-q * years);
  return type === "CE" ? spot * discQ * normCdf(d1) - strike * discR * normCdf(d2) : strike * discR * normCdf(-d2) - spot * discQ * normCdf(-d1);
}
