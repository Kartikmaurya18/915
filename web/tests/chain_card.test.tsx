import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import type { OptionsTopic } from "../src/api/types";
import { OptionChainCard } from "../src/components/OptionChainCard";
import { AllProviders } from "./providers";

const row = (strike: number, oi: number, atm = false) => ({
  strike, call_ltp: 100 + (24850 - strike) / 10, put_ltp: 100 - (24850 - strike) / 10, call_iv: 14.25, put_iv: 14.25, call_bid: 99, call_ask: 101, put_bid: 99, put_ask: 101,
  call_delta: 0.5, put_delta: -0.5, call_gamma: 0.001, put_gamma: 0.001, call_theta: -16.5, put_theta: -16.5, call_vega: 11, put_vega: 11,
  call_oi: oi, put_oi: oi * 2, call_oi_change: 1500, put_oi_change: -2500, call_volume: 10, put_volume: 10, combined_value: 200, is_atm: atm,
});
const OPTIONS = {
  underlying: "NIFTY", underlying_ltp: 24851, expiry: "22-Oct-2026", expiries: ["22-Oct-2026"], provider: "MOCK", greeks_source: "calculated", timestamp: "t",
  strikes: [24750, 24800, 24850, 24900, 24950].map((k, i) => row(k, 1000 * (i + 1), k === 24850)),
} as unknown as OptionsTopic;

const view = (options: OptionsTopic | undefined = OPTIONS, loading = false) =>
  render(
    <MemoryRouter>
      <AllProviders>
        <OptionChainCard options={options} atm={24850} loading={loading} />
      </AllProviders>
    </MemoryRouter>,
  );
const headers = () => screen.getAllByRole("columnheader").map((h) => h.textContent?.trim());
const strikes = () => screen.getAllByRole("row").slice(1).map((r) => r.querySelectorAll("td")[0]?.closest("tr")?.textContent ?? "");

beforeEach(() => localStorage.clear());
afterEach(cleanup);

describe("OptionChainCard", () => {
  it("shows price, delta, IV and OI by default, with the source of the Greeks", () => {
    view();
    expect(headers()).toEqual(expect.arrayContaining(["CE OI", "CE IV", "CE Δ", "CE LTP", "Strike", "PE LTP", "PE Δ", "PE IV", "PE OI"]));
    expect(headers()).not.toContain("CE Bid");
    expect(screen.getByText(/Greeks: calculated/)).toBeTruthy();
    expect(screen.getAllByText("14.3%").length).toBeGreaterThan(0);
  });

  it("columns can be added, appear on both sides, and are remembered", () => {
    view();
    fireEvent.click(screen.getByRole("checkbox", { name: "Theta per day" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "Best bid" }));
    expect(headers()).toEqual(expect.arrayContaining(["CE Θ", "PE Θ", "CE Bid", "PE Bid"]));
    expect(JSON.parse(localStorage.getItem("md_chain_columns")!)).toEqual(expect.arrayContaining(["theta", "bid"]));
    cleanup();
    view();
    expect(headers()).toContain("CE Θ"); // restored
  });

  it("the price column cannot be removed", () => {
    view();
    expect((screen.getByRole("checkbox", { name: "Last traded price" }) as HTMLInputElement).disabled).toBe(true);
  });

  it("ignores a corrupt saved preference", () => {
    localStorage.setItem("md_chain_columns", "{not json");
    view();
    expect(headers()).toContain("CE Δ");
    cleanup();
    localStorage.setItem("md_chain_columns", JSON.stringify(["gamma"])); // no price column: unusable
    view();
    expect(headers()).toContain("CE LTP");
  });

  it("sorts by a column header and announces it", () => {
    view();
    const oi = screen.getByRole("columnheader", { name: /CE OI/ });
    expect(oi.getAttribute("aria-sort")).toBe("none");
    fireEvent.click(within(oi).getByRole("button"));
    expect(oi.getAttribute("aria-sort")).toBe("descending");
    expect(strikes()[0]).toContain("24,950"); // largest call OI first
    fireEvent.click(within(oi).getByRole("button"));
    expect(oi.getAttribute("aria-sort")).toBe("ascending");
    expect(strikes()[0]).toContain("24,750");
  });

  it("finds a strike by typing part of it", () => {
    view();
    fireEvent.change(screen.getByRole("textbox", { name: "Find a strike" }), { target: { value: "249" } });
    expect(strikes()).toHaveLength(2);
    fireEvent.change(screen.getByRole("textbox", { name: "Find a strike" }), { target: { value: "99999" } });
    expect(screen.getByText("No matching strike")).toBeTruthy();
  });

  it("narrows to strikes around the money", () => {
    view();
    fireEvent.click(screen.getByRole("tab", { name: /^±3/ }));
    expect(strikes()).toHaveLength(5); // only five strikes exist, all within three of the money
    expect(screen.getByText(/Showing 5 of 5 strikes/)).toBeTruthy();
  });

  it("marks in-the-money cells in text as well as shade, and the money row as ATM", () => {
    view();
    const atmRow = strikes().find((r) => r.includes("24,850"))!;
    expect(atmRow).toContain("ATM");
    const rows = screen.getAllByRole("row").slice(1);
    const lowStrike = rows.find((r) => r.textContent?.includes("24,750"))!; // below spot: call ITM, put OTM
    expect(within(lowStrike).getAllByText(/in the money/)).toHaveLength(1);
  });

  it("selecting a contract from the chain still works", () => {
    view();
    fireEvent.click(screen.getByRole("checkbox", { name: "Select 24,900 CE" }));
    expect(screen.getByText(/Trade 1 selected/)).toBeTruthy();
  });

  it("shows loading and empty states", () => {
    view(undefined, true);
    expect(screen.getByText("Loading option chain")).toBeTruthy();
    cleanup();
    view({ ...OPTIONS, strikes: [] });
    expect(screen.getByText("No option chain yet")).toBeTruthy();
  });
});
