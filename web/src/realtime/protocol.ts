/** Wire protocol v1 for /ws/v1. Mirrors src/realtime/protocol.py. */
import type { Json, PatchNode } from "./patch";

export const CLOSE_AUTH = 4401;
export const CLOSE_CAPACITY = 4429;
export const CLOSE_SLOW = 4408;
export const CLOSE_PROTOCOL = 4400;

export interface SnapshotMsg {
  type: "snapshot";
  topic: string;
  epoch: number;
  seq: number;
  ts: number;
  pub: number;
  data: Json;
}
export interface PatchMsg {
  type: "patch";
  topic: string;
  epoch: number;
  seq: number;
  base: number;
  ts: number;
  pub: number;
  patch: PatchNode;
}
export interface HelloMsg { type: "hello"; heartbeat_s: number; max_topics: number }
export interface HeartbeatMsg { type: "heartbeat"; t: number; feed_age_ms: number | null }
export interface ErrorMsg { type: "error"; code: string; message: string; topic?: string }
export interface OtherMsg {
  type: "subscribed" | "unsubscribed" | "auth_ok" | "pong" | "rate_limited" | "paper_changed";
  [key: string]: unknown;
}
/** Several topic updates of one tick in a single message (only sent to clients that asked with `batch: true`). */
export interface BatchMsg { type: "batch"; frames: ServerMsg[] }
export type ServerMsg = SnapshotMsg | PatchMsg | HelloMsg | HeartbeatMsg | ErrorMsg | OtherMsg | BatchMsg;

export type ClientMsg =
  | { type: "auth"; token: string }
  | { type: "subscribe"; topics: string[]; batch?: boolean }
  | { type: "unsubscribe"; topics: string[] }
  | { type: "resync"; topic: string }
  | { type: "ping" };

export function parseServerMessage(raw: string): ServerMsg | null {
  try {
    const msg = JSON.parse(raw) as ServerMsg;
    return msg && typeof msg === "object" && typeof msg.type === "string" ? msg : null;
  } catch {
    return null;
  }
}
