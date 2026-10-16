import { useCallback, useEffect, useRef, useState } from "react";
import { useRealtimeClient } from "../realtime/RealtimeProvider";
import { api } from "./http";
import type { PaperState } from "./types";

/** With open positions the unrealised P&L moves with every tick, so refresh fairly often. */
export const POLL_OPEN_MS = 5000;
/** With none, nothing changes between events; the poll is only a safety net for a missed notice. */
export const POLL_IDLE_MS = 30000;
/** Notices arrive in bursts (several exits on one tick); refetch once after they settle. */
const NOTICE_DEBOUNCE_MS = 120;

/**
 * A user's own paper book. It is private, so it is NOT a broadcast topic: it is fetched over
 * authenticated REST. The server pushes a small "paper_changed" notice over the WebSocket whenever
 * the book changes (an order or close from any tab, an automatic take-profit/stop-loss exit), and the
 * browser refetches at once. Polling remains as a safety net and to keep the unrealised P&L of open
 * positions current: every 5 s while there are open positions, every 30 s otherwise, and only while
 * the tab is visible. Each poll is scheduled after the previous load finished, from the data it returned.
 */
export function usePaperState(): { state: PaperState | null; error: string | null; refresh: () => Promise<void> } {
  const [state, setState] = useState<PaperState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const inflight = useRef<AbortController | null>(null);
  const hasOpen = useRef(false);
  const client = useRealtimeClient();

  const load = useCallback(async () => {
    inflight.current?.abort();
    const ctrl = new AbortController();
    inflight.current = ctrl;
    try {
      const next = await api.get<PaperState>("/api/paper/state", ctrl.signal);
      if (!ctrl.signal.aborted) {
        hasOpen.current = next.open_positions.length > 0;
        setState(next);
        setError(null);
      }
    } catch (err) {
      if (!ctrl.signal.aborted) setError(err instanceof Error ? err.message : "Could not load your paper trades");
    }
  }, []);

  const refresh = load;

  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | null = null;
    let notice: ReturnType<typeof setTimeout> | null = null;
    let stopped = false;

    const schedule = () => {
      if (timer) clearTimeout(timer);
      if (stopped) return;
      timer = setTimeout(() => {
        const next = document.visibilityState === "visible" ? load() : Promise.resolve();
        void next.then(schedule);
      }, hasOpen.current ? POLL_OPEN_MS : POLL_IDLE_MS);
    };

    void load().then(schedule);
    const offNotice = client.onPaperChanged(() => {
      if (notice) clearTimeout(notice);
      notice = setTimeout(() => {
        if (timer) clearTimeout(timer);
        void load().then(schedule); // restarts the countdown from now, with the interval for the new state
      }, NOTICE_DEBOUNCE_MS);
    });
    const onVisible = () => {
      if (document.visibilityState === "visible") void load();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      stopped = true;
      if (timer) clearTimeout(timer);
      if (notice) clearTimeout(notice);
      offNotice();
      document.removeEventListener("visibilitychange", onVisible);
      inflight.current?.abort();
    };
  }, [load, client]);

  return { state, error, refresh };
}
