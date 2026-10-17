import { useEffect, useState } from "react";
import { ApiError, api } from "./http";
import type { PairCandles } from "./types";

const POLL_MS = 3000;

export interface PairState {
  data: PairCandles | null;
  /** The server answered 404: one of the strikes is not in the live chain. */
  unavailable: boolean;
  error: string | null;
}

/**
 * Candles of a CE + PE pair (a strangle, or a straddle when the strikes are equal). The first request for a
 * pair makes the server start tracking it, so the series begins then; the response says when. Refreshes every
 * few seconds while the tab is visible. The server caches briefly, so many viewers cost one engine call.
 */
export function usePairCandles(ceStrike: number | null, peStrike: number | null): PairState {
  const [state, setState] = useState<PairState>({ data: null, unavailable: false, error: null });

  useEffect(() => {
    if (ceStrike === null || peStrike === null) {
      setState({ data: null, unavailable: false, error: null });
      return;
    }
    const ctrl = new AbortController();
    const path = `/api/pair-candles?ce_strike=${encodeURIComponent(ceStrike)}&pe_strike=${encodeURIComponent(peStrike)}`;
    const load = async () => {
      try {
        const data = await api.get<PairCandles>(path, ctrl.signal);
        if (!ctrl.signal.aborted) setState({ data, unavailable: false, error: null });
      } catch (err) {
        if (ctrl.signal.aborted) return;
        if (err instanceof ApiError && err.status === 404) setState({ data: null, unavailable: true, error: null });
        else setState((s) => ({ ...s, error: err instanceof Error ? err.message : "Could not load candles" }));
      }
    };
    setState((s) => ({ data: s.data?.ce_strike === ceStrike && s.data?.pe_strike === peStrike ? s.data : null, unavailable: false, error: null }));
    void load();
    const timer = setInterval(() => {
      if (document.visibilityState === "visible") void load();
    }, POLL_MS);
    return () => {
      clearInterval(timer);
      ctrl.abort();
    };
  }, [ceStrike, peStrike]);

  return state;
}
