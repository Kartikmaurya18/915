import { act, cleanup, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { POLL_IDLE_MS, POLL_OPEN_MS, usePaperState } from "../src/api/usePaperState";
import { AuthProvider } from "../src/auth/AuthContext";
import { RealtimeClient } from "../src/realtime/client";
import { RealtimeProvider } from "../src/realtime/RealtimeProvider";
import { FakeSocket } from "./fakeSocket";

const book = (open: number) => ({
  simulated: true,
  config: { take_profit_percent: 10, stop_loss_percent: 5, take_profit_amount: null, stop_loss_amount: null },
  open_positions: Array.from({ length: open }, (_, i) => ({ id: "p" + i })),
  history: [],
  stats: {},
  pnl_summary: {},
});

let openCount: number;
let stateCalls: number;

function stubFetch() {
  stateCalls = 0;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      if (url.startsWith("/api/auth/me")) return new Response("{}", { status: 404 }); // auth disabled: signed in locally
      if (url.startsWith("/api/paper/state")) {
        stateCalls++;
        return new Response(JSON.stringify(book(openCount)), { status: 200, headers: { "Content-Type": "application/json" } });
      }
      return new Response("{}", { status: 404 });
    }),
  );
}

function Probe({ out }: { out: { open: number | null } }) {
  const { state } = usePaperState();
  out.open = state ? state.open_positions.length : null;
  return null;
}

function mount() {
  const client = new RealtimeClient({
    url: () => "ws://test/ws/v1",
    authRequired: () => false,
    getToken: () => null,
    WebSocketImpl: FakeSocket as unknown as typeof WebSocket,
  });
  const out = { open: null as number | null };
  render(
    <AuthProvider>
      <RealtimeProvider client={client}>
        <Probe out={out} />
      </RealtimeProvider>
    </AuthProvider>,
  );
  return { client, out };
}

beforeEach(() => {
  FakeSocket.reset();
  localStorage.clear();
  openCount = 0;
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

async function connected() {
  await waitFor(() => expect(FakeSocket.instances.length).toBe(1));
  FakeSocket.last.open();
  FakeSocket.last.receive({ type: "hello", heartbeat_s: 15, max_topics: 16 });
}

describe("usePaperState", () => {
  it("refetches as soon as the server says the book changed", async () => {
    stubFetch();
    const { out } = mount();
    await waitFor(() => expect(out.open).toBe(0));
    await connected();
    const before = stateCalls;
    openCount = 2; // e.g. an automatic exit or an order from another tab
    act(() => FakeSocket.last.receive({ type: "paper_changed" }));
    await waitFor(() => expect(out.open).toBe(2), { timeout: 2000 });
    expect(stateCalls).toBeGreaterThan(before);
  });

  it("a burst of notices causes a single refetch", async () => {
    stubFetch();
    const { out } = mount();
    await waitFor(() => expect(out.open).toBe(0));
    await connected();
    const before = stateCalls;
    act(() => {
      for (let i = 0; i < 25; i++) FakeSocket.last.receive({ type: "paper_changed" });
    });
    await new Promise((r) => setTimeout(r, 400));
    expect(stateCalls - before).toBe(1);
  });

  it("stops listening when the page goes away", async () => {
    stubFetch();
    mount();
    await waitFor(() => expect(stateCalls).toBe(1));
    await connected();
    cleanup();
    const before = stateCalls;
    FakeSocket.last.receive({ type: "paper_changed" });
    await new Promise((r) => setTimeout(r, 300));
    expect(stateCalls).toBe(before);
  });

  it("polls often while positions are open and rarely when none are", async () => {
    stubFetch();
    openCount = 1;
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { out } = mount();
    await vi.waitFor(() => expect(out.open).toBe(1));
    const base = stateCalls;
    await vi.advanceTimersByTimeAsync(POLL_OPEN_MS + 50);
    expect(stateCalls).toBe(base + 1); // an open book is refreshed every 5 s
    openCount = 0;
    await vi.advanceTimersByTimeAsync(POLL_OPEN_MS + 50);
    await vi.waitFor(() => expect(out.open).toBe(0));
    const idleBase = stateCalls;
    await vi.advanceTimersByTimeAsync(POLL_OPEN_MS * 3);
    expect(stateCalls).toBe(idleBase); // nothing open: no 5 s polling any more
    await vi.advanceTimersByTimeAsync(POLL_IDLE_MS);
    expect(stateCalls).toBeGreaterThan(idleBase); // but the safety-net poll still runs
  });
});
