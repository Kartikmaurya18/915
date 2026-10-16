import { useConnection } from "../realtime/RealtimeProvider";
import { fmtAge } from "../lib/format";

const LABEL = {
  idle: "Offline",
  connecting: "Connecting…",
  live: "Live",
  reconnecting: "Reconnecting…",
  auth_failed: "Signed out",
} as const;

/** Connection state, plus how old the market feed is when the ingestor/Redis stop delivering. */
export function StatusPill() {
  const { status, feedAgeMs } = useConnection();
  const stale = status === "live" && feedAgeMs !== null && feedAgeMs > 15_000;
  const tone = status === "live" && !stale ? "ok" : status === "auth_failed" ? "bad" : "warn";
  return (
    <span className={`pill pill-${tone}`} role="status" aria-live="polite" title={feedAgeMs === null ? undefined : `Feed age ${fmtAge(feedAgeMs)}`}>
      <span className="dot" aria-hidden="true" />
      {stale ? `Feed stale · ${fmtAge(feedAgeMs)}` : LABEL[status]}
    </span>
  );
}
