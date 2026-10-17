/**
 * What the screen should tell the user about the health of the prices it is showing. Pure, so every rule is tested.
 * The aim: a number on a trading screen is never mistaken for a live one when it is not.
 */
import type { IndexTopic } from "../api/types";
import { fmtAge } from "./format";

export type FeedState = "live" | "connecting" | "reconnecting" | "stale" | "offline" | "signed-out";

export interface HealthMessage {
  id: string;
  tone: "warn" | "info";
  text: string;
}

/** A chain older than this, while the market is open, is flagged. Generous: real providers refresh every few seconds. */
export const CHAIN_STALE_SECONDS = 30;
/** Warn when the provider's access token has less than this long to live. */
export const TOKEN_WARN_MINUTES = 60;

const SESSION_TEXT: Record<string, string> = {
  WEEKEND: "The market is closed for the weekend.",
  HOLIDAY: "The market is closed for a holiday.",
  PRE_OPEN: "The market has not opened yet.",
  CLOSED: "The market has closed for the day.",
};

const clock = new Intl.DateTimeFormat("en-IN", { timeZone: "Asia/Kolkata", hour: "2-digit", minute: "2-digit", hour12: false });

export function connectionMessage(state: FeedState, feedAgeMs: number | null): HealthMessage | null {
  switch (state) {
    case "stale":
      return { id: "connection", tone: "warn", text: `The market feed has not updated for ${fmtAge(feedAgeMs)}. Prices below may be out of date.` };
    case "reconnecting":
      return { id: "connection", tone: "warn", text: "Connection lost; reconnecting. Prices below may be out of date until it returns." };
    case "offline":
      return { id: "connection", tone: "warn", text: "You are offline. Prices below are the last ones received." };
    default:
      return null;
  }
}

/** Messages about the data itself, from the index topic. `nowMs` is injected so tests do not depend on the clock. */
export function dataMessages(index: IndexTopic | undefined, nowMs: number): HealthMessage[] {
  if (!index) return [];
  const out: HealthMessage[] = [];
  const provider = index.provider;
  const quality = index.data_quality;
  const live = provider ? !provider.is_simulated : false;

  if (provider?.status === "ERROR") {
    out.push({
      id: "provider-error",
      tone: "warn",
      text: `The market data provider reports a problem${provider.status_detail ? `: ${provider.status_detail}` : ""}. Prices may be missing or out of date.`,
    });
  }

  if (live && quality) {
    if (quality.market_open === false) {
      const why = SESSION_TEXT[quality.market_session] ?? "The market is closed.";
      out.push({ id: "market-closed", tone: "info", text: `${why} Prices shown are from the last session.` });
    } else if (quality.chain_age_seconds !== null && quality.chain_age_seconds !== undefined && quality.chain_age_seconds > CHAIN_STALE_SECONDS) {
      out.push({ id: "chain-stale", tone: "warn", text: `Option prices are ${fmtAge(quality.chain_age_seconds * 1000)} old: the chain has stopped refreshing.` });
    }
  }

  const expires = provider?.credentials_expire_at ? Date.parse(provider.credentials_expire_at) : NaN;
  if (live && !Number.isNaN(expires)) {
    const minutes = Math.floor((expires - nowMs) / 60_000);
    if (minutes <= 0) {
      out.push({ id: "token", tone: "warn", text: "The data provider's access token has expired. The feed will stop; renew the token and restart the ingestor." });
    } else if (minutes <= TOKEN_WARN_MINUTES) {
      out.push({ id: "token", tone: "warn", text: `The data provider's access token expires at ${clock.format(new Date(expires))} IST, in ${minutes} min. Renew it before then to keep the feed.` });
    }
  }
  return out;
}

export function healthMessages(state: FeedState, feedAgeMs: number | null, index: IndexTopic | undefined, nowMs: number): HealthMessage[] {
  const first = connectionMessage(state, feedAgeMs);
  return [...(first ? [first] : []), ...dataMessages(index, nowMs)];
}
