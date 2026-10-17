import { useCallback, useSyncExternalStore } from "react";
import { loadState, saveState, type WatchState } from "../lib/watchlist";

/**
 * One watchlist store for the whole app, kept in localStorage so lists survive reloads and follow the user across
 * tabs (the `storage` event fires in the other tabs). Module-level so the chain's "Watch" button and the watchlist
 * panel stay in step without a provider.
 */
let current: WatchState | null = null;
const listeners = new Set<() => void>();

const read = (): WatchState => (current ??= loadState());

function write(next: WatchState): void {
  current = next;
  saveState(next);
  listeners.forEach((l) => l());
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  const onStorage = (e: StorageEvent) => {
    if (e.key === "md_watchlists" || e.key === null) {
      current = null; // re-read what the other tab saved
      listener();
    }
  };
  window.addEventListener("storage", onStorage);
  return () => {
    listeners.delete(listener);
    window.removeEventListener("storage", onStorage);
  };
}

export function useWatchlists(): { state: WatchState; update: (change: (s: WatchState) => WatchState) => void } {
  const state = useSyncExternalStore(subscribe, read, read);
  const update = useCallback((change: (s: WatchState) => WatchState) => write(change(read())), []);
  return { state, update };
}

/** Test hook: forget the cached state so the next read comes from storage. */
export function resetWatchlistsForTests(): void {
  current = null;
}
