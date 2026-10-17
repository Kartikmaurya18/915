import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import Strategy from "../src/pages/Strategy";
import { RealtimeClient } from "../src/realtime/client";
import { RealtimeProvider } from "../src/realtime/RealtimeProvider";
import { FakeSocket } from "./fakeSocket";
import { AllProviders } from "./providers";

const json = (status: number, body: unknown) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

const row = (strike: number, atm: number) => {
  const d = strike - atm;
  const call = Math.max(100 - d * 0.5, 2);
  const put = Math.max(100 + d * 0.5, 2);
  return {
    strike, call_ltp: call, put_ltp: put, call_iv: 14, put_iv: 14, call_oi: 1, put_oi: 1, call_volume: 1, put_volume: 1,
    call_delta: 0.5, put_delta: -0.5, combined_value: call + put, is_atm: strike === atm,
  };
};
const chain = (expiry = "22-Oct-2026") => ({
  underlying: "NIFTY", underlying_ltp: 24850, expiry, expiries: [expiry], provider: "MOCK", greeks_source: "calculated",
  strikes: Array.from({ length: 11 }, (_, i) => row(24600 + i * 50, 24850)),
});
const index = { underlying: "NIFTY", price: 24850, atm: { strike: 24850 } };

let ws: FakeSocket;
let opens: { body: Record<string, unknown>; key: string | null }[];
let failStrike: number | null;

beforeEach(() => {
  FakeSocket.reset();
  opens = [];
  failStrike = null;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init: RequestInit = {}) => {
      if (url.startsWith("/api/auth/me")) return json(404, { detail: "Not found" });
      if (url.startsWith("/api/config")) return json(200, { underlying: "NIFTY", candle_intervals: [1, 5, 15] });
      if (url.startsWith("/api/paper/open")) {
        const body = JSON.parse(String(init.body));
        opens.push({ body, key: new Headers(init.headers).get("Idempotency-Key") });
        if (body.strike === failStrike) return json(400, { detail: "Contract is not in the live chain" });
        return json(200, { id: "p", ...body });
      }
      return json(404, { detail: "unmocked " + url });
    }),
  );
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

async function setup() {
  const client = new RealtimeClient({
    url: () => "ws://test/ws/v1",
    authRequired: () => false,
    getToken: () => null,
    WebSocketImpl: FakeSocket as unknown as typeof WebSocket,
  });
  render(
    <MemoryRouter>
      <AllProviders>
        <RealtimeProvider client={client}>
          <Strategy />
        </RealtimeProvider>
      </AllProviders>
    </MemoryRouter>,
  );
  await waitFor(() => expect(FakeSocket.instances.length).toBe(1));
  ws = FakeSocket.last;
  ws.open();
  ws.receive({ type: "hello", heartbeat_s: 15, max_topics: 16 });
}
let seq = 0;
const deliver = (topic: string, data: unknown) => act(() => ws.receive({ type: "snapshot", topic, epoch: 1, seq: ++seq, ts: 1, pub: 1, data }));
const ready = async () => {
  await setup();
  await waitFor(() => expect(screen.getByText("Iron condor")).toBeTruthy());
  await deliver("options:NIFTY", chain());
  await deliver("index:NIFTY", index);
  await waitFor(() => expect((screen.getByRole("button", { name: /Iron condor/ }) as HTMLButtonElement).disabled).toBe(false));
};
const summary = () => screen.getByLabelText("Strategy summary").textContent ?? "";
const reviewDialog = () => document.querySelector("dialog[aria-label='Review simulated strategy orders']") as HTMLElement;

describe("Strategy page", () => {
  it("starts empty and builds an iron condor from the live chain", async () => {
    await ready();
    expect(screen.getByText("No legs yet")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /Iron condor/ }));
    await waitFor(() => expect(screen.getByLabelText("Strategy summary")).toBeTruthy());
    // BUY 24650 PE, SELL 24750 PE, SELL 24950 CE, BUY 25050 CE, 50 units each (premiums from the chain above)
    expect(screen.getAllByRole("combobox", { name: /^Strike of/ })).toHaveLength(4);
    expect(summary()).toContain("Net credit");
    expect(summary()).toContain("Max loss");
    expect(summary()).not.toContain("Unlimited");
    expect(screen.getByRole("img", { name: /Profit and loss at expiry/ })).toBeTruthy();
  });

  it("flags a strategy whose loss has no limit and says why that matters here", async () => {
    await ready();
    fireEvent.click(screen.getByRole("button", { name: /Short straddle/ }));
    await waitFor(() => expect(summary()).toContain("Unlimited"));
    expect(screen.getByText(/does not model margin/)).toBeTruthy();
  });

  it("places each leg as its own simulated order with its own idempotency key", async () => {
    await ready();
    fireEvent.click(screen.getByRole("button", { name: /Long straddle/ }));
    fireEvent.click(await screen.findByRole("button", { name: /Review simulated orders/ }));
    await waitFor(() => expect(reviewDialog()).toBeTruthy());
    expect(within(reviewDialog()).getByText(/No real order is placed/)).toBeTruthy();
    fireEvent.click(within(reviewDialog()).getByRole("button", { name: /Place 2 simulated orders/ }));
    await waitFor(() => expect(opens).toHaveLength(2));
    expect(opens.map((o) => o.body.option_type).sort()).toEqual(["CE", "PE"]);
    expect(opens.every((o) => o.body.strike === 24850 && o.body.side === "BUY" && o.body.quantity === 50)).toBe(true);
    expect(opens.every((o) => o.body.expiry === "22-Oct-2026" && o.body.underlying === "NIFTY")).toBe(true);
    expect(new Set(opens.map((o) => o.key)).size).toBe(2);
    await waitFor(() => expect(within(reviewDialog()).getByText(/Executed 2 paper positions/)).toBeTruthy());
    // Placed legs leave the builder.
    fireEvent.click(within(reviewDialog()).getByRole("button", { name: "Done" }));
    await waitFor(() => expect(screen.getByText("No legs yet")).toBeTruthy());
  });

  it("keeps a failed leg in place, reuses its key on retry, and reports the failure", async () => {
    await ready();
    fireEvent.click(screen.getByRole("button", { name: /Long strangle/ }));
    fireEvent.click(await screen.findByRole("button", { name: /Review simulated orders/ }));
    await waitFor(() => expect(reviewDialog()).toBeTruthy());
    failStrike = 24950;
    fireEvent.click(within(reviewDialog()).getByRole("button", { name: /Place 2 simulated orders/ }));
    await waitFor(() => expect(within(reviewDialog()).getByText(/1 executed, 1 failed/)).toBeTruthy());
    const firstKey = opens.find((o) => o.body.strike === 24950)!.key;
    failStrike = null;
    fireEvent.click(within(reviewDialog()).getByRole("button", { name: /Place 1 simulated order$/ }));
    await waitFor(() => expect(opens).toHaveLength(3));
    expect(opens[2]!.key).toBe(firstKey); // a retry of the same order is safe to repeat
  });

  it("refuses to trade legs built for an expiry the chain has since left", async () => {
    await ready();
    fireEvent.click(screen.getByRole("button", { name: /Long call/ }));
    await screen.findByRole("button", { name: /Review simulated orders/ });
    await deliver("options:NIFTY", chain("29-Oct-2026"));
    await waitFor(() => expect(screen.getByRole("alert").textContent).toMatch(/moved to the 29-Oct-2026 expiry/));
    expect((screen.getByRole("button", { name: /Review simulated orders/ }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("repricing a leg to another strike reads that strike's price", async () => {
    await ready();
    fireEvent.click(screen.getByRole("button", { name: /Long call/ }));
    const select = (await screen.findByRole("combobox", { name: /^Strike of/ })) as HTMLSelectElement;
    const before = summary();
    fireEvent.change(select, { target: { value: "24900" } });
    await waitFor(() => expect(summary()).not.toBe(before));
    // Strike 24900 CE = 100 - 25 = 75; 50 units => debit 3,750
    expect(summary()).toContain("3,750.00");
  });

  it("explains instead of guessing when a preset needs strikes the chain lacks", async () => {
    await setup();
    await deliver("options:NIFTY", { ...chain(), strikes: chain().strikes.slice(4, 7) });
    await deliver("index:NIFTY", index);
    await waitFor(() => expect((screen.getByRole("button", { name: /Iron condor/ }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole("button", { name: /Iron condor/ }));
    expect((await screen.findByRole("alert")).textContent).toMatch(/further from the money/);
    expect(screen.getByText("No legs yet")).toBeTruthy();
  });
});
