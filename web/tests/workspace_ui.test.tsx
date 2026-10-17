import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { FeedBanner } from "../src/components/FeedBanner";
import { Shortcuts } from "../src/components/Shortcuts";
import { WatchlistPanel } from "../src/components/WatchlistPanel";
import Workspace from "../src/pages/Workspace";
import { RealtimeClient } from "../src/realtime/client";
import { RealtimeProvider } from "../src/realtime/RealtimeProvider";
import { resetWatchlistsForTests } from "../src/state/useWatchlists";
import { useUI } from "../src/ui/UIProvider";
import { FakeSocket } from "./fakeSocket";
import { AllProviders } from "./providers";

vi.mock("../src/components/CandleChart", () => ({
  CandleChart: ({ summary }: { summary: string }) => <div role="img" aria-label={summary} />,
  describeSeries: (name: string) => name,
}));

const json = (status: number, body: unknown) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
const row = (strike: number) => ({ strike, call_ltp: 120 - (strike - 24800) / 10, put_ltp: 80 + (strike - 24800) / 10, call_iv: 14, put_iv: 14, is_atm: strike === 24850 });
const chain = (expiry = "22-Oct-2026") => ({ underlying: "NIFTY", underlying_ltp: 24850, expiry, expiries: [expiry], provider: "MOCK", strikes: [24750, 24800, 24850, 24900, 24950].map(row) });
const index = { underlying: "NIFTY", price: 24851, atm: { strike: 24850 } };

let ws: FakeSocket;
let seq = 0;
const deliver = (topic: string, data: unknown) => act(() => ws.receive({ type: "snapshot", topic, epoch: 1, seq: ++seq, ts: 1, pub: 1, data }));

function Probe() {
  const loc = useLocation();
  const ui = useUI();
  return (
    <>
      <span data-testid="path">{loc.pathname}</span>
      <span data-testid="order">{String(ui.orderOpen)}</span>
      <span data-testid="palette">{String(ui.paletteOpen)}</span>
      <input aria-label="typing field" />
    </>
  );
}

async function mount(children: React.ReactNode) {
  const client = new RealtimeClient({ url: () => "ws://test/ws/v1", authRequired: () => false, getToken: () => null, WebSocketImpl: FakeSocket as unknown as typeof WebSocket });
  render(
    <MemoryRouter initialEntries={["/workspace"]}>
      <AllProviders>
        <RealtimeProvider client={client}>
          <Probe />
          {children}
        </RealtimeProvider>
      </AllProviders>
    </MemoryRouter>,
  );
  await waitFor(() => expect(FakeSocket.instances.length).toBe(1));
  ws = FakeSocket.last;
  ws.open();
  ws.receive({ type: "hello", heartbeat_s: 15, max_topics: 16 });
}
const feed = async (expiry?: string) => {
  await deliver("options:NIFTY", chain(expiry));
  await deliver("index:NIFTY", index);
};

beforeEach(() => {
  FakeSocket.reset();
  seq = 0;
  localStorage.clear();
  resetWatchlistsForTests();
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      if (url.startsWith("/api/auth/me")) return json(404, { detail: "Not found" });
      if (url.startsWith("/api/config")) return json(200, { underlying: "NIFTY", candle_intervals: [1, 5, 15] });
      return json(404, { detail: "unmocked" });
    }),
  );
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("Workspace layout", () => {
  it("shows two charts by default, switches to one or four, and remembers the choice", async () => {
    await mount(<Workspace />);
    await screen.findByRole("region", { name: "Chart 2" });
    expect(screen.queryByRole("region", { name: "Chart 3" })).toBeNull();
    fireEvent.click(screen.getByRole("tab", { name: /^4 \(Four/ }));
    expect(screen.getByRole("region", { name: "Chart 4" })).toBeTruthy();
    expect(JSON.parse(localStorage.getItem("md_workspace")!).layout).toBe("4");
    fireEvent.click(screen.getByRole("tab", { name: /^1 \(One chart/ }));
    expect(screen.queryByRole("region", { name: "Chart 2" })).toBeNull();
    cleanup();
    FakeSocket.reset();
    await mount(<Workspace />);
    await screen.findByRole("region", { name: "Chart 1" });
    expect(screen.getByRole("tab", { name: /^1 \(One chart/ }).getAttribute("aria-selected")).toBe("true"); // restored
  });

  it("each pane chooses its own chart, and a changed pane type persists without disturbing the others", async () => {
    await mount(<Workspace />);
    await feed();
    const pane2 = await screen.findByRole("region", { name: "Chart 2" });
    fireEvent.change(within(pane2).getByRole("combobox", { name: /Chart 2/ }), { target: { value: "strangle" } });
    expect(await within(screen.getByRole("region", { name: "Chart 2" })).findByLabelText("Strangle chart")).toBeTruthy();
    expect(JSON.parse(localStorage.getItem("md_workspace")!).panes.slice(0, 2)).toEqual(["price", "strangle"]);
    expect(within(screen.getByRole("region", { name: "Chart 1" })).getByRole("combobox", { name: /Chart 1/ })).toHaveProperty("value", "price");
  });

  it("ignores a corrupt saved layout", async () => {
    localStorage.setItem("md_workspace", "{{{");
    await mount(<Workspace />);
    expect(await screen.findByRole("region", { name: "Chart 2" })).toBeTruthy();
  });
});

describe("WatchlistPanel", () => {
  it("adds a contract, shows its live price, and the move since it was added", async () => {
    await mount(<WatchlistPanel />);
    await feed();
    await waitFor(() => expect((screen.getByRole("button", { name: "Add contract" }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole("button", { name: "Add contract" })); // ATM 24850 CE = 120 - 5 = 115
    const table = await screen.findByRole("table");
    expect(within(table).getByText("NIFTY 24850 CE")).toBeTruthy();
    expect(within(table).getByText("115.00")).toBeTruthy();
    await deliver("options:NIFTY", { ...chain(), strikes: chain().strikes.map((r) => (r.strike === 24850 ? { ...r, call_ltp: 126.5 } : r)) });
    expect(await within(table).findByText("126.50")).toBeTruthy();
    expect(within(table).getByText(/\+10\.00\s*%/)).toBeTruthy(); // 115 -> 126.5
    expect(JSON.parse(localStorage.getItem("md_watchlists")!).lists[0].items).toHaveLength(1);
  });

  it("does not list the same contract twice and removes on request", async () => {
    await mount(<WatchlistPanel />);
    await feed();
    await waitFor(() => expect((screen.getByRole("button", { name: "Add contract" }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole("button", { name: "Add contract" }));
    fireEvent.click(screen.getByRole("button", { name: "Add contract" }));
    expect(screen.getAllByRole("button", { name: /^Remove NIFTY/ })).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: /^Remove NIFTY 24850 CE/ }));
    expect(screen.getByText("Nothing watched yet")).toBeTruthy();
  });

  it("shows 'Different expiry' instead of a price once the chain rolls to another expiry", async () => {
    await mount(<WatchlistPanel />);
    await feed();
    await waitFor(() => expect((screen.getByRole("button", { name: "Add contract" }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole("button", { name: "Add contract" }));
    await deliver("options:NIFTY", chain("29-Oct-2026"));
    expect(await screen.findByText("Different expiry")).toBeTruthy();
    expect(screen.queryByText("115.00")).toBeNull();
  });

  it("keeps several lists, switching between them", async () => {
    await mount(<WatchlistPanel />);
    await feed();
    fireEvent.click(screen.getByRole("button", { name: /New list/ }));
    const picker = screen.getByRole("combobox", { name: "Choose watchlist" }) as HTMLSelectElement;
    expect([...picker.options].map((o) => o.text)).toEqual(["Watchlist 1 (0)", "Watchlist 2 (0)"]);
    expect(picker.selectedOptions[0]!.text).toBe("Watchlist 2 (0)");
  });

  it("survives a reload: lists come back from storage", async () => {
    await mount(<WatchlistPanel />);
    await feed();
    await waitFor(() => expect((screen.getByRole("button", { name: "Add contract" }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole("button", { name: "Add contract" }));
    cleanup();
    FakeSocket.reset();
    resetWatchlistsForTests();
    await mount(<WatchlistPanel />);
    expect(await screen.findByText("NIFTY 24850 CE")).toBeTruthy();
  });
});

describe("keyboard shortcuts", () => {
  it("g then a letter navigates, n opens a new order, / opens search, ? lists the shortcuts", async () => {
    await mount(<Shortcuts />);
    fireEvent.keyDown(window, { key: "g" });
    fireEvent.keyDown(window, { key: "s" });
    expect(screen.getByTestId("path").textContent).toBe("/strategy");
    fireEvent.keyDown(window, { key: "n" });
    expect(screen.getByTestId("order").textContent).toBe("true");
    fireEvent.keyDown(window, { key: "/" });
    await waitFor(() => expect(screen.getByTestId("palette").textContent).toBe("true"));
  });

  it("opens the help list with ? and describes every shortcut", async () => {
    await mount(<Shortcuts />);
    fireEvent.keyDown(window, { key: "?" });
    const help = await waitFor(() => {
      const d = document.querySelector("dialog[aria-label='Keyboard shortcuts']") as HTMLElement;
      expect(d.querySelector("h2")).toBeTruthy();
      return d;
    });
    expect(help.textContent).toContain("Go to Workspace");
    expect(help.textContent).toContain("New simulated order");
  });

  it("does nothing while typing in a field", async () => {
    await mount(<Shortcuts />);
    const field = screen.getByLabelText("typing field");
    field.focus();
    fireEvent.keyDown(field, { key: "n" });
    fireEvent.keyDown(field, { key: "g" });
    fireEvent.keyDown(field, { key: "s" });
    expect(screen.getByTestId("order").textContent).toBe("false");
    expect(screen.getByTestId("path").textContent).toBe("/workspace");
  });

  it("a stray g followed by another key cancels instead of arming the next letter", async () => {
    await mount(<Shortcuts />);
    fireEvent.keyDown(window, { key: "g" });
    fireEvent.keyDown(window, { key: "x" });
    fireEvent.keyDown(window, { key: "s" });
    expect(screen.getByTestId("path").textContent).toBe("/workspace");
  });
});

describe("FeedBanner", () => {
  it("is silent while the feed is live and warns when it goes stale", async () => {
    await mount(<FeedBanner />);
    act(() => ws.receive({ type: "heartbeat", t: 1, feed_age_ms: 500 }));
    expect(screen.queryByTestId("feed-banner")).toBeNull();
    act(() => ws.receive({ type: "heartbeat", t: 2, feed_age_ms: 42_000 }));
    expect((await screen.findByTestId("feed-banner")).textContent).toMatch(/may be out of date/);
    act(() => ws.receive({ type: "heartbeat", t: 3, feed_age_ms: 300 }));
    await waitFor(() => expect(screen.queryByTestId("feed-banner")).toBeNull());
  });

  it("warns when the connection drops", async () => {
    await mount(<FeedBanner />);
    act(() => ws.close(1006));
    expect((await screen.findByTestId("feed-banner")).textContent).toMatch(/reconnecting|offline/i);
  });
});
