import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./http";
import type { PaperState } from "./types";

const POLL_MS = 3000;

/**
 * A user's own paper book. It is private, so it is NOT a broadcast topic: it is fetched over
 * authenticated REST, every few seconds while the tab is visible and immediately after any action.
 */
export function usePaperState(): { state: PaperState | null; error: string | null; refresh: () => Promise<void> } {
  const [state, setState] = useState<PaperState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const inflight = useRef<AbortController | null>(null);

  const refresh = useCallback(async () => {
    inflight.current?.abort();
    const ctrl = new AbortController();
    inflight.current = ctrl;
    try {
      const next = await api.get<PaperState>("/api/paper/state", ctrl.signal);
      if (!ctrl.signal.aborted) {
        setState(next);
        setError(null);
      }
    } catch (err) {
      if (!ctrl.signal.aborted) setError(err instanceof Error ? err.message : "Could not load your paper trades");
    }
  }, []);

  useEffect(() => {
    void refresh();
    const timer = setInterval(() => {
      if (document.visibilityState === "visible") void refresh();
    }, POLL_MS);
    const onVisible = () => {
      if (document.visibilityState === "visible") void refresh();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisible);
      inflight.current?.abort();
    };
  }, [refresh]);

  return { state, error, refresh };
}
