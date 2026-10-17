/**
 * Framework-free /ws/v1 client: connection lifecycle, topic state, patch application.
 *
 * Responsibilities
 *  - authenticate with the first-frame `auth` message and renew the token in-band;
 *  - hold a reference count per topic and (un)subscribe on the wire only on 0 <-> 1 transitions;
 *  - keep the latest state per topic, applying snapshots and patches in `(epoch, seq)` order;
 *  - ask for a `resync` whenever a patch does not apply cleanly (gap, wrong epoch, bad node);
 *  - reconnect with jittered exponential backoff and detect a silent connection (no heartbeat).
 *
 * React reads it through `useTopic` / `useConnection` (see hooks.ts); nothing here imports React.
 */
import { applyPatch, PatchError, type Json } from "./patch";
import {
  CLOSE_AUTH,
  CLOSE_CAPACITY,
  parseServerMessage,
  type BatchMsg,
  type ClientMsg,
  type PatchMsg,
  type ServerMsg,
  type SnapshotMsg,
} from "./protocol";

export type ConnectionStatus = "idle" | "connecting" | "live" | "reconnecting" | "auth_failed";

export interface TopicState {
  epoch: number;
  seq: number;
  data: Json;
  /** Source (exchange/engine tick) time, epoch ms. */
  ts: number;
  /** When the ingestor published it, epoch ms. */
  pub: number;
  /** performance-independent receive time, epoch ms. */
  receivedAt: number;
}

export interface ConnectionInfo {
  status: ConnectionStatus;
  /** Time since the gateway last heard any market update (from the heartbeat); null if unknown. */
  feedAgeMs: number | null;
  reconnects: number;
  frames: number;
  bytes: number;
  snapshots: number;
  patches: number;
  resyncs: number;
  lastMessageAt: number | null;
}

export interface ClientOptions {
  url: () => string;
  /** False when the server runs with AUTH_MODE=disabled: no auth frame is sent. */
  authRequired: () => boolean;
  getToken: () => string | null;
  /** Called on close code 4401. Resolve true if a fresh token is now available. */
  onAuthFailure?: () => Promise<boolean>;
  WebSocketImpl?: typeof WebSocket;
  now?: () => number;
}

const INITIAL_BACKOFF_MS = 500;
const MAX_BACKOFF_MS = 10_000;
const CAPACITY_BACKOFF_MS = 5_000;
const WATCHDOG_INTERVAL_MS = 5_000;
const DEFAULT_HEARTBEAT_S = 15;

type Listener = () => void;

export class RealtimeClient {
  private opts: ClientOptions;
  private ws: WebSocket | null = null;
  private wanted = false;
  private refs = new Map<string, number>();
  private states = new Map<string, TopicState>();
  private resyncPending = new Set<string>();
  private topicListeners = new Map<string, Set<Listener>>();
  private infoListeners = new Set<Listener>();
  private paperListeners = new Set<Listener>();
  private backoff = INITIAL_BACKOFF_MS;
  private retryTimer: ReturnType<typeof setTimeout> | null = null;
  private watchdog: ReturnType<typeof setInterval> | null = null;
  private heartbeatS = DEFAULT_HEARTBEAT_S;
  private info: ConnectionInfo = {
    status: "idle",
    feedAgeMs: null,
    reconnects: 0,
    frames: 0,
    bytes: 0,
    snapshots: 0,
    patches: 0,
    resyncs: 0,
    lastMessageAt: null,
  };

  constructor(opts: ClientOptions) {
    this.opts = opts;
  }

  // -- public API ----------------------------------------------------------------------

  start(): void {
    if (this.wanted) return;
    this.wanted = true;
    this.open();
  }

  stop(): void {
    this.wanted = false;
    this.clearTimers();
    const ws = this.ws;
    this.ws = null;
    if (ws) {
      ws.onopen = ws.onmessage = ws.onclose = ws.onerror = null;
      try {
        ws.close(1000, "client_stop");
      } catch {
        /* already closed */
      }
    }
    this.states.clear();
    this.resyncPending.clear();
    this.setInfo({ status: "idle", feedAgeMs: null });
  }

  /** Call when the access token changed so the server keeps the socket open past the old expiry. */
  renewToken(): void {
    if (!this.opts.authRequired() || !this.isOpen()) return;
    const token = this.opts.getToken();
    if (token) this.send({ type: "auth", token });
  }

  /** Register interest in a topic. Returns the matching unsubscribe function. */
  subscribe(topic: string, listener: Listener): () => void {
    let set = this.topicListeners.get(topic);
    if (!set) this.topicListeners.set(topic, (set = new Set()));
    set.add(listener);
    const count = (this.refs.get(topic) ?? 0) + 1;
    this.refs.set(topic, count);
    if (count === 1 && this.isOpen()) this.send({ type: "subscribe", topics: [topic], batch: true });
    let active = true;
    return () => {
      if (!active) return;
      active = false;
      set!.delete(listener);
      const left = (this.refs.get(topic) ?? 1) - 1;
      if (left <= 0) {
        this.refs.delete(topic);
        this.states.delete(topic);
        this.resyncPending.delete(topic);
        if (this.isOpen()) this.send({ type: "unsubscribe", topics: [topic] });
        this.emitTopic(topic);
      } else {
        this.refs.set(topic, left);
      }
    };
  }

  getTopic(topic: string): TopicState | undefined {
    return this.states.get(topic);
  }

  getInfo(): ConnectionInfo {
    return this.info;
  }

  /**
   * The server says this user's paper book changed (an order, a close, a take-profit or stop-loss exit,
   * or a settings change, possibly from another tab). The notice carries no data: refetch over REST.
   */
  onPaperChanged(listener: Listener): () => void {
    this.paperListeners.add(listener);
    return () => this.paperListeners.delete(listener);
  }

  onInfo(listener: Listener): () => void {
    this.infoListeners.add(listener);
    return () => this.infoListeners.delete(listener);
  }

  // -- connection lifecycle -------------------------------------------------------------

  private isOpen(): boolean {
    return this.ws !== null && this.ws.readyState === 1;
  }

  private now(): number {
    return (this.opts.now ?? Date.now)();
  }

  private open(): void {
    if (!this.wanted) return;
    const Impl = this.opts.WebSocketImpl ?? WebSocket;
    this.setInfo({ status: this.info.reconnects > 0 ? "reconnecting" : "connecting" });
    let ws: WebSocket;
    try {
      ws = new Impl(this.opts.url());
    } catch {
      this.scheduleRetry();
      return;
    }
    this.ws = ws;
    ws.onopen = () => this.onOpen(ws);
    ws.onmessage = (event) => this.onMessage(ws, String(event.data));
    ws.onclose = (event) => this.onClose(ws, event.code);
    ws.onerror = () => {
      /* a close event always follows; reconnection is handled there */
    };
  }

  private onOpen(ws: WebSocket): void {
    if (ws !== this.ws) return;
    if (this.opts.authRequired()) {
      const token = this.opts.getToken();
      if (!token) {
        ws.close(CLOSE_AUTH, "no_token");
        return;
      }
      this.send({ type: "auth", token });
    }
    const topics = [...this.refs.keys()];
    if (topics.length) this.send({ type: "subscribe", topics, batch: true }); // one message per tick, not one per topic
    this.startWatchdog();
  }

  private onClose(ws: WebSocket, code: number): void {
    if (ws !== this.ws) return;
    this.ws = null;
    this.stopWatchdog();
    this.resyncPending.clear();
    if (!this.wanted) return;
    if (code === CLOSE_AUTH && this.opts.authRequired()) {
      this.setInfo({ status: "reconnecting" });
      void this.recoverAuth();
      return;
    }
    this.scheduleRetry(code === CLOSE_CAPACITY ? CAPACITY_BACKOFF_MS : 0);
  }

  private async recoverAuth(): Promise<void> {
    let ok = false;
    try {
      ok = (await this.opts.onAuthFailure?.()) ?? false;
    } catch {
      ok = false;
    }
    if (!this.wanted) return;
    if (ok) {
      this.open();
    } else {
      this.setInfo({ status: "auth_failed" });
    }
  }

  private scheduleRetry(minDelayMs = 0): void {
    if (!this.wanted || this.retryTimer) return;
    const delay = Math.max(minDelayMs, this.backoff) * (1 + Math.random() * 0.5);
    this.backoff = Math.min(this.backoff * 2, MAX_BACKOFF_MS);
    this.setInfo({ status: "reconnecting", reconnects: this.info.reconnects + 1 });
    this.retryTimer = setTimeout(() => {
      this.retryTimer = null;
      this.open();
    }, delay);
  }

  private startWatchdog(): void {
    this.stopWatchdog();
    this.watchdog = setInterval(() => {
      const last = this.info.lastMessageAt;
      const silentFor = last === null ? 0 : this.now() - last;
      if (this.ws && last !== null && silentFor > (this.heartbeatS * 2 + 5) * 1000) {
        // Connected but nothing arrives (half-open socket, dead proxy): force a reconnect.
        this.ws.close(4000, "watchdog");
      }
    }, WATCHDOG_INTERVAL_MS);
  }

  private stopWatchdog(): void {
    if (this.watchdog) clearInterval(this.watchdog);
    this.watchdog = null;
  }

  private clearTimers(): void {
    this.stopWatchdog();
    if (this.retryTimer) clearTimeout(this.retryTimer);
    this.retryTimer = null;
  }

  private send(msg: ClientMsg): void {
    if (this.isOpen()) this.ws!.send(JSON.stringify(msg));
  }

  // -- inbound messages ----------------------------------------------------------------

  private onMessage(ws: WebSocket, raw: string): void {
    if (ws !== this.ws) return;
    const msg = parseServerMessage(raw);
    const at = this.now();
    this.info = { ...this.info, frames: this.info.frames + 1, bytes: this.info.bytes + raw.length, lastMessageAt: at };
    if (msg === null) {
      this.emitInfo();
      return;
    }
    if (msg.type === "batch") {
      const inner = (msg as BatchMsg).frames;
      if (Array.isArray(inner)) for (const frame of inner) if (frame && typeof frame === "object") this.dispatch(frame, at);
      this.emitInfo();
      return;
    }
    this.dispatch(msg, at);
  }

  /** Handle one logical frame, whether it arrived alone or inside a batch. */
  private dispatch(msg: ServerMsg, at: number): void {
    switch (msg.type) {
      case "hello":
        this.backoff = INITIAL_BACKOFF_MS;
        this.heartbeatS = (msg as { heartbeat_s?: number }).heartbeat_s ?? DEFAULT_HEARTBEAT_S;
        this.setInfo({ status: "live" });
        return;
      case "snapshot":
        this.onSnapshot(msg as SnapshotMsg, at);
        return;
      case "patch":
        this.onPatch(msg as PatchMsg, at);
        return;
      case "heartbeat":
        this.setInfo({ feedAgeMs: (msg as { feed_age_ms: number | null }).feed_age_ms });
        return;
      case "paper_changed":
        for (const l of [...this.paperListeners]) l();
        this.emitInfo();
        return;
      default:
        this.emitInfo();
    }
  }

  private onSnapshot(msg: SnapshotMsg, at: number): void {
    const held = this.states.get(msg.topic);
    if (held && (msg.epoch < held.epoch || (msg.epoch === held.epoch && msg.seq < held.seq))) return;
    this.resyncPending.delete(msg.topic);
    if (!this.refs.has(msg.topic)) return; // unsubscribed while the frame was in flight
    this.states.set(msg.topic, { epoch: msg.epoch, seq: msg.seq, data: msg.data, ts: msg.ts, pub: msg.pub, receivedAt: at });
    this.info = { ...this.info, snapshots: this.info.snapshots + 1 };
    this.emitTopic(msg.topic);
    this.emitInfo();
  }

  private onPatch(msg: PatchMsg, at: number): void {
    const held = this.states.get(msg.topic);
    if (held && msg.epoch === held.epoch && msg.seq <= held.seq) return; // duplicate or stale
    if (!held || msg.epoch !== held.epoch || msg.base !== held.seq) {
      this.requestResync(msg.topic);
      return;
    }
    let data: Json;
    try {
      data = applyPatch(held.data, msg.patch);
    } catch (err) {
      if (err instanceof PatchError || err instanceof TypeError) {
        this.requestResync(msg.topic);
        return;
      }
      throw err;
    }
    this.states.set(msg.topic, { epoch: msg.epoch, seq: msg.seq, data, ts: msg.ts, pub: msg.pub, receivedAt: at });
    this.info = { ...this.info, patches: this.info.patches + 1 };
    this.emitTopic(msg.topic);
    this.emitInfo();
  }

  private requestResync(topic: string): void {
    if (this.resyncPending.has(topic) || !this.refs.has(topic)) return;
    this.resyncPending.add(topic);
    this.info = { ...this.info, resyncs: this.info.resyncs + 1 };
    this.send({ type: "resync", topic });
    this.emitInfo();
  }

  // -- change notification ------------------------------------------------------------

  private setInfo(patch: Partial<ConnectionInfo>): void {
    this.info = { ...this.info, ...patch };
    this.emitInfo();
  }

  private emitInfo(): void {
    for (const l of this.infoListeners) l();
  }

  private emitTopic(topic: string): void {
    const set = this.topicListeners.get(topic);
    if (set) for (const l of [...set]) l();
  }
}

export type { ServerMsg };
