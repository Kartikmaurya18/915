import { useConnection } from "../realtime/RealtimeProvider";
import { fmtAge } from "../lib/format";
import { cn } from "../ui/cn";

export type FeedState = "live" | "connecting" | "reconnecting" | "stale" | "offline" | "signed-out";

/** The connection, summarised. Shared by the pill (what you see) and the announcer (what a screen reader hears). */
export function feedState(status: string, feedAgeMs: number | null): FeedState {
  if (status === "auth_failed") return "signed-out";
  if (status === "live") return feedAgeMs !== null && feedAgeMs > 15_000 ? "stale" : "live";
  if (status === "connecting") return "connecting";
  if (status === "reconnecting") return "reconnecting";
  return "offline";
}

const LABEL: Record<FeedState, string> = {
  live: "Live",
  connecting: "Connecting",
  reconnecting: "Reconnecting",
  stale: "Feed stale",
  offline: "Offline",
  "signed-out": "Signed out",
};

const TONE: Record<FeedState, string> = {
  live: "bg-pos-soft text-pos",
  connecting: "bg-warn-soft text-warn",
  reconnecting: "bg-warn-soft text-warn",
  stale: "bg-warn-soft text-warn",
  offline: "bg-surface-2 text-muted",
  "signed-out": "bg-neg-soft text-neg",
};

/** Connection pill: a dot, a word and (when relevant) the age of the data. Never colour alone. */
export function StatusPill() {
  const { status, feedAgeMs } = useConnection();
  const state = feedState(status, feedAgeMs);
  return (
    <span
      data-state={state}
      title={feedAgeMs === null ? undefined : `Feed age ${fmtAge(feedAgeMs)}`}
      className={cn("inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-medium", TONE[state])}
    >
      <span aria-hidden="true" className={cn("size-2 rounded-full bg-current", state === "live" && "motion-safe:animate-pulse")} />
      <span>{LABEL[state]}</span>
      {state === "stale" && <span className="num font-normal">· {fmtAge(feedAgeMs)}</span>}
    </span>
  );
}
