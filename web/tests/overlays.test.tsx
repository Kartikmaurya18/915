import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setAccessToken } from "../src/api/http";
import CommandPalette, { matches } from "../src/components/CommandPalette";
import OrderDrawer, { quantityError } from "../src/components/OrderDrawer";
import { RealtimeClient } from "../src/realtime/client";
import { RealtimeProvider } from "../src/realtime/RealtimeProvider";
import { useUI } from "../src/ui/UIProvider";
import { FakeSocket } from "./fakeSocket";
import { AllProviders } from "./providers";

const json = (status: number, body: unknown) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

const row = (strike: number, call: number, put: number, atm = false) => ({
  strike, call_ltp: call, put_ltp: put, call_oi: 1000, put_oi: 1000, call_volume: 1, put_volume: 1, call_delta: 0.5, put_delta: -0.5, call_iv: null, put_iv: null, combined_value: call + put, is_atm: atm,
});
const CHAIN = { underlying: "NIFTY", underlying_ltp: 24850, expiry: "22-Oct-2026", expiries: ["22-Oct-2026"], provider: "MOCK", strikes: [row(24800, 120, 80), row(24850, 100, 95, true), row(24900, 70, 130)] };

type Call = { url: string; init: RequestInit };
let calls: Call[];
let openResponses: Response[];

function stubFetch() {
  calls = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init: RequestInit = {}) => {
      calls.push({ url, init });
      if (url.startsWith("/api/auth/me")) return json(404, { detail: "Not found" });
      if (url.startsWith("/api/config")) return json(200, { underlying: "NIFTY", candle_intervals: [1, 5, 15] });
      if (url.startsWith("/api/paper/open")) return openResponses.shift() ?? json(200, filled);
      return json(404, { detail: "unmocked " + url });
    }),
  );
}

const filled = { id: "p1", option_type: "PE", side: "BUY", strike: 24850, quantity: 50, entry_price: 95, take_profit_price: 104.5, stop_loss_price: 90.25 };

function Harness() {
  const ui = useUI();
  const location = useLocation();
  return (
    <>
      <span data-testid="path">{location.pathname}</span>
      <button onClick={() => ui.openOrder({ optionType: "PE", strike: 24850 })}>open-order</button>
      <button onClick={() => ui.openOrder()}>open-blank-order</button>
      {ui.paletteLoaded && <CommandPalette />}
      {ui.orderLoaded && <OrderDrawer />}
    </>
  );
}

let ws: FakeSocket;

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
          <Harness />
        </RealtimeProvider>
      </AllProviders>
    </MemoryRouter>,
  );
  await waitFor(() => expect(FakeSocket.instances.length).toBe(1));
  ws = FakeSocket.last;
  ws.open();
  ws.receive({ type: "hello", heartbeat_s: 15, max_topics: 16 });
}

const deliverChain = () => act(() => ws.receive({ type: "snapshot", topic: "options:NIFTY", epoch: 1, seq: 1, ts: 1, pub: 1, data: CHAIN }));
// Selected by label rather than ARIA role: jsdom (unlike a browser) does not expose a closed <dialog> by role.
const dialogByLabel = (label: string) => {
  const el = document.querySelector<HTMLDialogElement>(`dialog[aria-label="${label}"]`);
  if (!el) throw new Error(`no dialog labelled ${label}`);
  return el;
};
const palette = () => dialogByLabel("Search and commands");
const drawer = () => dialogByLabel("New simulated order");

beforeEach(() => {
  FakeSocket.reset();
  localStorage.clear();
  openResponses = [];
  stubFetch();
});
afterEach(() => {
  cleanup();
  setAccessToken(null);
  vi.unstubAllGlobals();
  document.documentElement.classList.remove("dark");
});

describe("matches", () => {
  const c = { label: "24,850 CE (at the money)", detail: "100.00", keywords: "trade buy strike 24850 call", group: "Contracts" as const };
  it("needs every typed word, ignoring case and thousands separators", () => {
    expect(matches(c, "24850")).toBe(true);
    expect(matches(c, "24,850 ce")).toBe(true);
    expect(matches(c, "CALL money")).toBe(true);
    expect(matches(c, "24850 put")).toBe(false);
    expect(matches(c, "")).toBe(true);
  });
  it("also searches the group name", () => {
    expect(matches(c, "contracts")).toBe(true);
  });
});

describe("command palette", () => {
  async function openPalette() {
    await setup();
    fireEvent.keyDown(window, { key: "k", ctrlKey: true });
    await screen.findByRole("combobox");
    await deliverChain();
  }

  it("opens and closes with Ctrl+K (and Cmd+K), and focuses the search box", async () => {
    await setup();
    fireEvent.keyDown(window, { key: "k", ctrlKey: true });
    const input = await screen.findByRole("combobox", { name: "Search strikes, pages and actions" });
    expect(palette().hasAttribute("open")).toBe(true);
    expect(document.activeElement).toBe(input);
    fireEvent.keyDown(window, { key: "k", metaKey: true });
    await waitFor(() => expect(palette().hasAttribute("open")).toBe(false));
  });

  it("is a proper combobox: listbox, options, active descendant", async () => {
    await openPalette();
    const input = screen.getByRole("combobox");
    expect(input.getAttribute("aria-expanded")).toBe("true");
    const list = screen.getByRole("listbox", { name: "Results" });
    expect(input.getAttribute("aria-controls")).toBe(list.id);
    const options = within(list).getAllByRole("option");
    expect(options[0]!.getAttribute("aria-selected")).toBe("true");
    expect(input.getAttribute("aria-activedescendant")).toBe(options[0]!.id);
  });

  it("filters strikes as you type (24800 finds 24,800) and groups the results", async () => {
    await openPalette();
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "24800" } });
    const options = within(screen.getByRole("listbox")).getAllByRole("option").map((o) => o.textContent ?? "");
    expect(options.some((t) => t.includes("24,800 CE"))).toBe(true);
    expect(options.some((t) => t.includes("24,800 PE"))).toBe(true);
    expect(options.some((t) => t.includes("24,850"))).toBe(false);
    expect(screen.getByRole("group", { name: "Contracts" })).toBeTruthy();
  });

  it("moves with the arrow keys, wrapping around", async () => {
    await openPalette();
    const input = screen.getByRole("combobox");
    fireEvent.change(input, { target: { value: "24800" } });
    const opts = () => within(screen.getByRole("listbox")).getAllByRole("option");
    expect(opts()[0]!.getAttribute("aria-selected")).toBe("true");
    fireEvent.keyDown(input, { key: "ArrowDown" });
    expect(opts()[1]!.getAttribute("aria-selected")).toBe("true");
    expect(input.getAttribute("aria-activedescendant")).toBe(opts()[1]!.id);
    fireEvent.keyDown(input, { key: "ArrowUp" });
    fireEvent.keyDown(input, { key: "ArrowUp" });
    expect(opts()[opts().length - 1]!.getAttribute("aria-selected")).toBe("true"); // wrapped to the last
  });

  it("Enter on a contract closes the palette and opens the order panel for exactly that contract", async () => {
    await openPalette();
    const input = screen.getByRole("combobox");
    fireEvent.change(input, { target: { value: "24900" } });
    fireEvent.keyDown(input, { key: "ArrowDown" }); // CE is first, PE second
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() => expect(drawer().hasAttribute("open")).toBe(true));
    expect(palette().hasAttribute("open")).toBe(false);
    await deliverChain();
    expect((within(drawer()).getByLabelText("Strike") as HTMLSelectElement).value).toBe("24900");
    expect(within(drawer()).getByRole("tab", { name: /Put option/ }).getAttribute("aria-selected")).toBe("true");
  });

  it("Enter on a page navigates there and closes", async () => {
    await openPalette();
    const input = screen.getByRole("combobox");
    fireEvent.change(input, { target: { value: "paper trading" } });
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() => expect(screen.getByTestId("path").textContent).toBe("/trading"));
    expect(palette().hasAttribute("open")).toBe(false);
  });

  it("can change the theme from the palette", async () => {
    await openPalette();
    const input = screen.getByRole("combobox");
    fireEvent.change(input, { target: { value: "theme" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(localStorage.getItem("md_theme")).toBe("light"); // system -> light
  });

  it("says so, helpfully, when nothing matches", async () => {
    await openPalette();
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "zzzzzz" } });
    expect(screen.getByText(/Nothing matches/)).toBeTruthy();
    expect(screen.getByRole("combobox").getAttribute("aria-activedescendant")).toBeNull();
    expect(screen.getByText("No results")).toBeTruthy(); // announced to screen readers
  });

  it("closes on Escape and on a click on the backdrop", async () => {
    await openPalette();
    fireEvent(palette(), new Event("cancel", { cancelable: true })); // what the browser sends for Escape
    await waitFor(() => expect(palette().hasAttribute("open")).toBe(false));
    fireEvent.keyDown(window, { key: "k", ctrlKey: true });
    await waitFor(() => expect(palette().hasAttribute("open")).toBe(true));
    fireEvent.click(palette()); // the backdrop is the dialog element itself
    await waitFor(() => expect(palette().hasAttribute("open")).toBe(false));
  });

  it("ignores the shortcut with extra modifiers (Ctrl+Shift+K belongs to the browser)", async () => {
    await setup();
    fireEvent.keyDown(window, { key: "k", ctrlKey: true, shiftKey: true });
    expect(screen.queryByRole("combobox")).toBeNull();
  });
});

describe("quantityError", () => {
  it("accepts whole numbers from 1 to 100,000 only", () => {
    expect(quantityError("50")).toBeNull();
    expect(quantityError("1")).toBeNull();
    expect(quantityError("100000")).toBeNull();
    expect(quantityError("")).toBe("Enter a quantity.");
    expect(quantityError("0")).toBe("Quantity must be at least 1.");
    expect(quantityError("2.5")).toBe("Quantity must be a whole number.");
    expect(quantityError("100001")).toMatch(/cannot exceed/);
  });
});

describe("order drawer", () => {
  async function openDrawer() {
    await setup();
    fireEvent.click(screen.getByText("open-order"));
    await waitFor(() => expect(drawer().hasAttribute("open")).toBe(true));
    await deliverChain();
    return within(drawer());
  }
  const reviewButton = (d: ReturnType<typeof within>) => d.getByRole("button", { name: "Review order" }) as HTMLButtonElement;

  it("opens pre-filled from the contract and says it is a simulation", async () => {
    const d = await openDrawer();
    expect((d.getByLabelText("Strike") as HTMLSelectElement).value).toBe("24850");
    expect(d.getByRole("tab", { name: /Put option/ }).getAttribute("aria-selected")).toBe("true");
    expect(d.getAllByText("Simulated").length).toBeGreaterThan(0);
    expect(d.getByText(/No real order is placed/)).toBeTruthy();
    expect(d.getByText("₹ 4,750.00")).toBeTruthy(); // 95 x 50
  });

  it("shows the live price and updates the estimate as the quantity changes", async () => {
    const d = await openDrawer();
    fireEvent.change(d.getByLabelText("Quantity"), { target: { value: "100" } });
    expect(d.getByText("₹ 9,500.00")).toBeTruthy();
    fireEvent.click(d.getByRole("tab", { name: /Call option/ }));
    expect(d.getByText("₹ 10,000.00")).toBeTruthy(); // the call price is 100
  });

  it("validates inline and keeps the main button disabled until the order is valid", async () => {
    const d = await openDrawer();
    expect(reviewButton(d).disabled).toBe(false);
    fireEvent.change(d.getByLabelText("Quantity"), { target: { value: "" } });
    expect(d.getByRole("alert").textContent).toBe("Enter a quantity.");
    expect(d.getByLabelText("Quantity").getAttribute("aria-invalid")).toBe("true");
    expect(reviewButton(d).disabled).toBe(true);
    fireEvent.change(d.getByLabelText("Quantity"), { target: { value: "0" } });
    expect(d.getByRole("alert").textContent).toBe("Quantity must be at least 1.");
    fireEvent.change(d.getByLabelText("Quantity"), { target: { value: "75" } });
    expect(d.queryByRole("alert")).toBeNull();
    expect(reviewButton(d).disabled).toBe(false);
  });

  it("strips non-digits and steps the quantity with the +/- buttons within its limits", async () => {
    const d = await openDrawer();
    const input = d.getByLabelText("Quantity") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "1a2" } });
    expect(input.value).toBe("12");
    fireEvent.click(d.getByRole("button", { name: "Increase quantity" }));
    expect(input.value).toBe("13");
    fireEvent.change(input, { target: { value: "1" } });
    expect((d.getByRole("button", { name: "Decrease quantity" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(d.getByRole("button", { name: "50" })); // a quick quantity
    expect(input.value).toBe("50");
  });

  it("has a review step that restates the order and warns that it is a simulation", async () => {
    const d = await openDrawer();
    fireEvent.click(reviewButton(d));
    expect(d.getByRole("heading", { name: "Review order" })).toBeTruthy();
    expect(d.getByText("24,850 PE")).toBeTruthy();
    expect(d.getByText(/This is a simulation/)).toBeTruthy();
    fireEvent.click(d.getByRole("button", { name: "Back" }));
    expect(d.getByRole("heading", { name: "New order" })).toBeTruthy();
  });

  it("places the order once, with the contract's identity and an Idempotency-Key, then confirms", async () => {
    const d = await openDrawer();
    fireEvent.click(reviewButton(d));
    fireEvent.click(d.getByRole("button", { name: "Place simulated order" }));
    await d.findByText("Simulated order placed");
    const post = calls.filter((c) => c.url === "/api/paper/open");
    expect(post).toHaveLength(1);
    expect(JSON.parse(post[0]!.init.body as string)).toEqual({ option_type: "PE", side: "BUY", quantity: 50, strike: 24850, expiry: "22-Oct-2026", underlying: "NIFTY" });
    expect((post[0]!.init.headers as Record<string, string>)["Idempotency-Key"]).toMatch(/^[0-9a-f-]{36}$/);
    expect(d.getByText("Entry price")).toBeTruthy();
    fireEvent.click(d.getByRole("button", { name: "Done" }));
    await waitFor(() => expect(drawer().hasAttribute("open")).toBe(false));
  });

  it("shows a server rejection, keeps the order, and a retry reuses the same key (safe to retry)", async () => {
    openResponses = [json(400, { detail: "No price for PE 24850." }), json(200, filled)];
    const d = await openDrawer();
    fireEvent.click(reviewButton(d));
    fireEvent.click(d.getByRole("button", { name: "Place simulated order" }));
    expect((await d.findByRole("alert")).textContent).toContain("No price for PE 24850.");
    expect(d.getByRole("heading", { name: "Review order" })).toBeTruthy(); // still here, nothing lost
    fireEvent.click(d.getByRole("button", { name: "Place simulated order" }));
    await d.findByText("Simulated order placed");
    const keys = calls.filter((c) => c.url === "/api/paper/open").map((c) => (c.init.headers as Record<string, string>)["Idempotency-Key"]);
    expect(keys).toHaveLength(2);
    expect(keys[0]).toBe(keys[1]);
  });

  it("editing the order starts a new idempotency key", async () => {
    openResponses = [json(400, { detail: "boom" }), json(200, filled)];
    const d = await openDrawer();
    fireEvent.click(reviewButton(d));
    fireEvent.click(d.getByRole("button", { name: "Place simulated order" }));
    await d.findByRole("alert");
    fireEvent.click(d.getByRole("button", { name: "Back" }));
    fireEvent.change(d.getByLabelText("Quantity"), { target: { value: "60" } });
    fireEvent.click(reviewButton(d));
    fireEvent.click(d.getByRole("button", { name: "Place simulated order" }));
    await d.findByText("Simulated order placed");
    const keys = calls.filter((c) => c.url === "/api/paper/open").map((c) => (c.init.headers as Record<string, string>)["Idempotency-Key"]);
    expect(keys[0]).not.toBe(keys[1]);
  });

  it("lets you place another order after a fill", async () => {
    const d = await openDrawer();
    fireEvent.click(reviewButton(d));
    fireEvent.click(d.getByRole("button", { name: "Place simulated order" }));
    await d.findByText("Simulated order placed");
    fireEvent.click(d.getByRole("button", { name: "Place another" }));
    expect(d.getByRole("heading", { name: "New order" })).toBeTruthy();
  });

  it("blocks ordering while there is no market data, and says why", async () => {
    await setup();
    fireEvent.click(screen.getByText("open-blank-order"));
    await waitFor(() => expect(drawer().hasAttribute("open")).toBe(true));
    const d = within(drawer());
    expect(d.getByText("Waiting for the option chain…")).toBeTruthy();
    expect(reviewButton(d).disabled).toBe(true);
    expect((d.getByLabelText("Strike") as HTMLSelectElement).disabled).toBe(true);
  });

  it("closes from the X button and the Cancel button", async () => {
    const d = await openDrawer();
    fireEvent.click(d.getByRole("button", { name: "Close order panel" }));
    await waitFor(() => expect(drawer().hasAttribute("open")).toBe(false));
    fireEvent.click(screen.getByText("open-order"));
    await waitFor(() => expect(drawer().hasAttribute("open")).toBe(true));
    fireEvent.click(within(drawer()).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(drawer().hasAttribute("open")).toBe(false));
  });
});
