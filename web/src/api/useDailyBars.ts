import { useEffect, useState } from "react";
import { aggregateDaily, type StoredCandle } from "../lib/daily";
import type { Bar } from "./types";
import { api } from "./http";

/** The most history one request returns (25 fifteen-minute candles a day, so about eight months). */
const LIMIT = 5000;

/**
 * One bar per trading day, built from the stored 15-minute candles. Loaded once, only when `enabled`
 * (the daily ranges are selected). The server caches this read briefly, so many viewers cost one engine call.
 */
export function useDailyBars(enabled: boolean): { bars: Bar[]; loading: boolean; error: string | null } {
  const [bars, setBars] = useState<Bar[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!enabled || bars !== null) return;
    const ctrl = new AbortController();
    api
      .get<{ candles: StoredCandle[] }>(`/api/history/candles?interval=15&limit=${LIMIT}`, ctrl.signal)
      .then((res) => {
        setBars(aggregateDaily(res.candles ?? []));
        setError(null);
      })
      .catch((err: unknown) => {
        if (!ctrl.signal.aborted) setError(err instanceof Error ? err.message : "Could not load price history");
      });
    return () => ctrl.abort();
  }, [enabled, bars]);

  return { bars: bars ?? [], loading: enabled && bars === null && error === null, error };
}
