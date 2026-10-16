import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { RealtimeClient } from "../src/realtime/client";
import { FakeSocket } from "./fakeSocket";

const snapshot = (topic: string, seq: number, data: unknown, epoch = 1) => ({ type: "snapshot", topic, epoch, seq, ts: 1, pub: 2, data });
const patch = (topic: string, seq: number, base: number, node: unknown, epoch = 1) => ({ type: "patch", topic, epoch, seq, base, ts: 1, pub: 2, patch: node });

function make(over: Partial<ConstructorParameters<typeof RealtimeClient>[0]> = {}) {
  let token: string | null = "tok-1";
  const client = new RealtimeClient({
    url: () => "ws://test/ws/v1",
    authRequired: () => true,
    getToken: () => token,
    WebSocketImpl: FakeSocket as unknown as typeof WebSocket,
    ...over,
  });
  return { client, setToken: (t: string | null) => (token = t) };
}

function connect(client: RealtimeClient): FakeSocket {
  client.start();
  const ws = FakeSocket.last;
  ws.open();
  ws.receive({ type: "hello", heartbeat_s: 15, max_topics: 16 });
  return ws;
}

beforeEach(() => {
  FakeSocket.reset();
  vi.useFakeTimers();
});
afterEach(() => {
  vi.useRealTimers();
});

describe("handshake and subscriptions", () => {
  it("sends the auth frame first, then the wanted topics", () => {
    const { client } = make();
    client.subscribe("index:NIFTY", () => undefined);
    client.start();
    const ws = FakeSocket.last;
    ws.open();
    expect(ws.sent[0]).toEqual({ type: "auth", token: "tok-1" });
    expect(ws.sent[1]).toEqual({ type: "subscribe", topics: ["index:NIFTY"] });
    expect(ws.url).not.toContain("tok-1"); // never in the URL
  });

  it("sends no auth frame when the server runs without auth", () => {
    const { client } = make({ authRequired: () => false });
    client.start();
    FakeSocket.last.open();
    expect(FakeSocket.last.sentTypes()).not.toContain("auth");
  });

  it("subscribes on the wire only when the first listener arrives, unsubscribes when the last leaves", () => {
    const { client } = make();
    const ws = connect(client);
    const off1 = client.subscribe("options:NIFTY", () => undefined);
    const off2 = client.subscribe("options:NIFTY", () => undefined);
    expect(ws.sent.filter((m) => m.type === "subscribe")).toHaveLength(1);
    off1();
    expect(ws.sentTypes()).not.toContain("unsubscribe");
    off2();
    expect(ws.sent.at(-1)).toEqual({ type: "unsubscribe", topics: ["options:NIFTY"] });
  });

  it("an unsubscribe function is safe to call twice", () => {
    const { client } = make();
    const ws = connect(client);
    const off = client.subscribe("index:NIFTY", () => undefined);
    off();
    off();
    expect(ws.sent.filter((m) => m.type === "unsubscribe")).toHaveLength(1);
  });

  it("sends a fresh token on renewal so the socket outlives the old expiry", () => {
    const { client, setToken } = make();
    const ws = connect(client);
    setToken("tok-2");
    client.renewToken();
    expect(ws.sent.at(-1)).toEqual({ type: "auth", token: "tok-2" });
  });
});

describe("topic state", () => {
  it("applies a snapshot, then an adjacent patch, and notifies listeners", () => {
    const { client } = make();
    const ws = connect(client);
    const listener = vi.fn();
    client.subscribe("index:NIFTY", listener);
    ws.receive(snapshot("index:NIFTY", 1, { price: 100, atm: { strike: 24800 } }));
    expect(client.getTopic("index:NIFTY")?.data).toEqual({ price: 100, atm: { strike: 24800 } });
    ws.receive(patch("index:NIFTY", 2, 1, ["d", { price: 101 }, []]));
    expect(client.getTopic("index:NIFTY")).toMatchObject({ seq: 2, data: { price: 101, atm: { strike: 24800 } } });
    expect(listener).toHaveBeenCalledTimes(2);
    expect(client.getInfo()).toMatchObject({ snapshots: 1, patches: 1, resyncs: 0 });
  });

  it("asks for a resync when a patch skips a version, and keeps the old state until the snapshot", () => {
    const { client } = make();
    const ws = connect(client);
    client.subscribe("index:NIFTY", () => undefined);
    ws.receive(snapshot("index:NIFTY", 1, { price: 100 }));
    ws.receive(patch("index:NIFTY", 5, 4, ["d", { price: 105 }, []])); // base 4, we hold 1
    expect(ws.sent.at(-1)).toEqual({ type: "resync", topic: "index:NIFTY" });
    expect(client.getTopic("index:NIFTY")?.data).toEqual({ price: 100 });
    ws.receive(snapshot("index:NIFTY", 5, { price: 105 }));
    expect(client.getTopic("index:NIFTY")?.data).toEqual({ price: 105 });
  });

  it("requests only one resync while one is outstanding", () => {
    const { client } = make();
    const ws = connect(client);
    client.subscribe("index:NIFTY", () => undefined);
    ws.receive(snapshot("index:NIFTY", 1, { v: 1 }));
    ws.receive(patch("index:NIFTY", 5, 4, ["d", { v: 5 }, []]));
    ws.receive(patch("index:NIFTY", 6, 5, ["d", { v: 6 }, []]));
    expect(ws.sent.filter((m) => m.type === "resync")).toHaveLength(1);
  });

  it("treats a new epoch as needing a snapshot, and accepts that snapshot", () => {
    const { client } = make();
    const ws = connect(client);
    client.subscribe("index:NIFTY", () => undefined);
    ws.receive(snapshot("index:NIFTY", 7, { v: 7 }, 1));
    ws.receive(patch("index:NIFTY", 1, 0, ["d", { v: 1 }, []], 2)); // new leader, patch for a base we don't have
    expect(ws.sent.at(-1)).toEqual({ type: "resync", topic: "index:NIFTY" });
    ws.receive(snapshot("index:NIFTY", 1, { v: "new" }, 2));
    expect(client.getTopic("index:NIFTY")).toMatchObject({ epoch: 2, seq: 1, data: { v: "new" } });
  });

  it("ignores duplicate and older messages", () => {
    const { client } = make();
    const ws = connect(client);
    client.subscribe("index:NIFTY", () => undefined);
    ws.receive(snapshot("index:NIFTY", 3, { v: 3 }));
    ws.receive(patch("index:NIFTY", 3, 2, ["d", { v: 99 }, []]));
    ws.receive(snapshot("index:NIFTY", 2, { v: 2 }));
    expect(client.getTopic("index:NIFTY")?.data).toEqual({ v: 3 });
    expect(ws.sentTypes()).not.toContain("resync");
  });

  it("resyncs instead of crashing on a corrupt patch", () => {
    const { client } = make();
    const ws = connect(client);
    client.subscribe("index:NIFTY", () => undefined);
    ws.receive(snapshot("index:NIFTY", 1, { v: 1 }));
    ws.receive(patch("index:NIFTY", 2, 1, ["bogus"]));
    expect(ws.sent.at(-1)).toEqual({ type: "resync", topic: "index:NIFTY" });
    expect(client.getTopic("index:NIFTY")?.data).toEqual({ v: 1 });
  });

  it("drops a snapshot for a topic nobody listens to any more", () => {
    const { client } = make();
    const ws = connect(client);
    const off = client.subscribe("index:NIFTY", () => undefined);
    off();
    ws.receive(snapshot("index:NIFTY", 1, { v: 1 }));
    expect(client.getTopic("index:NIFTY")).toBeUndefined();
  });

  it("ignores garbage frames", () => {
    const { client } = make();
    const ws = connect(client);
    ws.onmessage?.({ data: "not json" });
    ws.onmessage?.({ data: "[1,2]" });
    expect(client.getInfo().status).toBe("live");
  });

  it("records the gateway's feed age from heartbeats", () => {
    const { client } = make();
    const ws = connect(client);
    ws.receive({ type: "heartbeat", t: 1, feed_age_ms: 4200 });
    expect(client.getInfo().feedAgeMs).toBe(4200);
  });
});

describe("reconnection", () => {
  it("reconnects with backoff, resubscribes, and refetches state from a fresh snapshot", () => {
    const { client } = make();
    const first = connect(client);
    client.subscribe("index:NIFTY", () => undefined);
    first.receive(snapshot("index:NIFTY", 1, { v: 1 }));

    first.serverClose(1006);
    expect(client.getInfo().status).toBe("reconnecting");
    expect(FakeSocket.instances).toHaveLength(1);
    vi.advanceTimersByTime(1_000);
    expect(FakeSocket.instances).toHaveLength(2);

    const second = FakeSocket.last;
    second.open();
    expect(second.sent[0]).toEqual({ type: "auth", token: "tok-1" });
    expect(second.sent[1]).toEqual({ type: "subscribe", topics: ["index:NIFTY"] });
    second.receive({ type: "hello", heartbeat_s: 15, max_topics: 16 });
    expect(client.getInfo().status).toBe("live");
    second.receive(snapshot("index:NIFTY", 9, { v: 9 }));
    expect(client.getTopic("index:NIFTY")?.data).toEqual({ v: 9 });
  });

  it("keeps retrying after repeated failures, with a capped delay", () => {
    const { client } = make();
    client.start();
    for (let i = 0; i < 12; i++) {
      FakeSocket.last.serverClose(1006);
      vi.advanceTimersByTime(16_000); // longer than the maximum jittered delay
    }
    expect(FakeSocket.instances.length).toBe(13);
    FakeSocket.last.serverClose(1006);
    vi.advanceTimersByTime(15_500); // cap is 10 s plus up to 50% jitter
    expect(FakeSocket.instances.length).toBe(14);
  });

  it("on auth close (4401) refreshes the token and reconnects immediately", async () => {
    const onAuthFailure = vi.fn(async () => true);
    const { client } = make({ onAuthFailure });
    const ws = connect(client);
    ws.serverClose(4401);
    await vi.advanceTimersByTimeAsync(0);
    expect(onAuthFailure).toHaveBeenCalledTimes(1);
    expect(FakeSocket.instances).toHaveLength(2);
  });

  it("gives up with auth_failed when the token cannot be renewed", async () => {
    const { client } = make({ onAuthFailure: async () => false });
    const ws = connect(client);
    ws.serverClose(4401);
    await vi.advanceTimersByTimeAsync(60_000);
    expect(client.getInfo().status).toBe("auth_failed");
    expect(FakeSocket.instances).toHaveLength(1);
  });

  it("waits longer after a capacity rejection (4429)", () => {
    const { client } = make();
    const ws = connect(client);
    ws.serverClose(4429);
    vi.advanceTimersByTime(4_000);
    expect(FakeSocket.instances).toHaveLength(1);
    vi.advanceTimersByTime(4_000);
    expect(FakeSocket.instances).toHaveLength(2);
  });

  it("forces a reconnect when a live socket goes silent", () => {
    let now = 1_000_000;
    const { client } = make({ now: () => now });
    const ws = connect(client);
    now += 60_000; // more than 2 x heartbeat + 5 s with no frames
    vi.advanceTimersByTime(5_000);
    expect(ws.closedWith?.reason).toBe("watchdog");
  });

  it("does not reconnect after stop()", () => {
    const { client } = make();
    const ws = connect(client);
    client.stop();
    ws.serverClose(1006);
    vi.advanceTimersByTime(30_000);
    expect(FakeSocket.instances).toHaveLength(1);
    expect(client.getInfo().status).toBe("idle");
  });
});
