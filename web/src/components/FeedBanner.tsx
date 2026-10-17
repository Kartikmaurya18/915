import { AlertTriangle } from "lucide-react";
import { fmtAge } from "../lib/format";
import { useConnection } from "../realtime/RealtimeProvider";
import { feedState } from "./StatusPill";

/**
 * A visible warning, shown only while prices on screen may be out of date. The pill in the header says the same,
 * but a number on a trading screen should never be mistaken for a live one, so the page itself says it too.
 * Screen readers are told once by the live announcer, so this is not itself a live region.
 */
export function FeedBanner() {
  const { status, feedAgeMs } = useConnection();
  const state = feedState(status, feedAgeMs);
  if (state === "live" || state === "connecting" || state === "signed-out") return null;
  const text =
    state === "stale"
      ? `The market feed has not updated for ${fmtAge(feedAgeMs)}. Prices below may be out of date.`
      : state === "reconnecting"
        ? "Connection lost; reconnecting. Prices below may be out of date until it returns."
        : "You are offline. Prices below are the last ones received.";
  return (
    <p className="flex items-start gap-2 rounded-control border border-line bg-warn-soft px-3 py-2 text-sm text-warn" data-testid="feed-banner">
      <AlertTriangle aria-hidden="true" className="mt-0.5 size-4 shrink-0" />
      <span>{text}</span>
    </p>
  );
}
