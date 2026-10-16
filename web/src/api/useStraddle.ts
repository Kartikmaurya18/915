import { useEffect, useState } from "react";
import { ApiError, api } from "./http";
import type { StraddleCandles } from "./types";

const POLL_MS = 3000;

export interface StraddleState {
  data: StraddleCandles | null;
  /** True once the server answered 404: this strike has no candles (yet). */
  noData: boolean;
  error: string | null;
}

/**
 * CE/PE/Straddle candles for one strike. For today (no `date`) it refreshes every few seconds while the
 * tab is visible; for a past date the data is final, so it is fetched once. The server caches the answer
 * briefly, so many viewers of the same strike cost one engine call, not one each.
 *
 * `initial` lets a page that already holds the data (the history bundle) show it without a request.
 */
export function useStraddleCandles(strike: number | null, date?: string, initial?: StraddleCandles | null): StraddleState {
  const [state, setState] = useState<StraddleState>({ data: null, noData: false, error: null });

  useEffect(() => {
    if (strike === null) {
      setState({ data: null, noData: false, error: null });
      return;
    }
    if (initial && initial.strike === strike) {
      setState({ data: initial, noData: false, error: null });
      return;
    }
    const ctrl = new AbortController();
    const path = `/api/straddle-candles?strike=${encodeURIComponent(strike)}${date ? `&date=${encodeURIComponent(date)}` : ""}`;
    const load = async () => {
      try {
        const data = await api.get<StraddleCandles>(path, ctrl.signal);
        if (!ctrl.signal.aborted) setState({ data, noData: false, error: null });
      } catch (err) {
        if (ctrl.signal.aborted) return;
        if (err instanceof ApiError && err.status === 404) setState((s) => ({ data: s.data?.strike === strike ? s.data : null, noData: true, error: null }));
        else setState((s) => ({ ...s, error: err instanceof Error ? err.message : "Could not load candles" }));
      }
    };
    setState((s) => ({ data: s.data?.strike === strike ? s.data : null, noData: false, error: null }));
    void load();
    if (date) return () => ctrl.abort();
    const timer = setInterval(() => {
      if (document.visibilityState === "visible") void load();
    }, POLL_MS);
    return () => {
      clearInterval(timer);
      ctrl.abort();
    };
  }, [strike, date, initial]);

  return state;
}
