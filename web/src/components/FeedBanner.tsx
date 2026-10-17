import { useEffect, useState } from "react";
import { AlertTriangle, Info } from "lucide-react";
import type { IndexTopic } from "../api/types";
import { useConfig } from "../api/useConfig";
import { healthMessages } from "../lib/feedHealth";
import { useConnection, useTopic } from "../realtime/RealtimeProvider";
import { cn } from "../ui/cn";
import { feedState } from "./StatusPill";

/**
 * Visible warnings, shown only while the prices on screen may not be what they seem: a stale or dropped connection,
 * a provider error, a closed market, an option chain that has stopped refreshing, a data token about to expire.
 * The header pill says some of this, but a number on a trading screen should never be mistaken for a live one, so
 * the page itself says it too. Screen readers are told about connection changes by the live announcer, so this is
 * not itself a live region.
 */
export function FeedBanner() {
  const { status, feedAgeMs } = useConnection();
  const { config } = useConfig();
  const index = useTopic<IndexTopic>(config ? `index:${config.underlying}` : null).data;

  // Re-evaluated every 30 s so a token that is about to expire is noticed even when no new tick arrives.
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 30_000);
    return () => clearInterval(t);
  }, []);

  const messages = healthMessages(feedState(status, feedAgeMs), feedAgeMs, index, now);
  if (messages.length === 0) return null;
  return (
    <div className="grid gap-2" data-testid="feed-banner">
      {messages.map((m) => (
        <p key={m.id} className={cn("flex items-start gap-2 rounded-control border border-line px-3 py-2 text-sm", m.tone === "warn" ? "bg-warn-soft text-warn" : "bg-surface-2 text-fg")}>
          {m.tone === "warn" ? <AlertTriangle aria-hidden="true" className="mt-0.5 size-4 shrink-0" /> : <Info aria-hidden="true" className="mt-0.5 size-4 shrink-0" />}
          <span>{m.text}</span>
        </p>
      ))}
    </div>
  );
}
