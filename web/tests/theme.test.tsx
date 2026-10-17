import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ThemeProvider, useTheme } from "../src/theme/ThemeProvider";

type Listener = (e: { matches: boolean }) => void;
let systemDark = false;
let listeners: Listener[] = [];

function stubMatchMedia() {
  vi.stubGlobal("matchMedia", (query: string) => ({
    matches: query.includes("dark") ? systemDark : false,
    media: query,
    addEventListener: (_: string, l: Listener) => listeners.push(l),
    removeEventListener: (_: string, l: Listener) => (listeners = listeners.filter((x) => x !== l)),
  }));
}

function Probe() {
  const { mode, resolved, cycle, setMode } = useTheme();
  return (
    <div>
      <span data-testid="mode">{mode}</span>
      <span data-testid="resolved">{resolved}</span>
      <button onClick={cycle}>cycle</button>
      <button onClick={() => setMode("dark")}>dark</button>
    </div>
  );
}
const mount = () =>
  render(
    <ThemeProvider>
      <Probe />
    </ThemeProvider>,
  );
const isDark = () => document.documentElement.classList.contains("dark");

beforeEach(() => {
  systemDark = false;
  listeners = [];
  localStorage.clear();
  document.documentElement.classList.remove("dark");
  stubMatchMedia();
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("ThemeProvider", () => {
  it("follows the system by default", () => {
    systemDark = true;
    mount();
    expect(screen.getByTestId("mode").textContent).toBe("system");
    expect(screen.getByTestId("resolved").textContent).toBe("dark");
    expect(isDark()).toBe(true);
  });

  it("tracks the system while the user has not chosen", () => {
    mount();
    expect(isDark()).toBe(false);
    act(() => listeners.forEach((l) => l({ matches: true })));
    expect(isDark()).toBe(true);
    act(() => listeners.forEach((l) => l({ matches: false })));
    expect(isDark()).toBe(false);
  });

  it("cycles system, light, dark and back, applying and saving each choice", () => {
    systemDark = true;
    mount();
    const click = () => act(() => screen.getByText("cycle").click());
    click();
    expect(screen.getByTestId("mode").textContent).toBe("light");
    expect(isDark()).toBe(false); // an explicit choice beats the system
    expect(localStorage.getItem("md_theme")).toBe("light");
    click();
    expect(screen.getByTestId("mode").textContent).toBe("dark");
    expect(isDark()).toBe(true);
    click();
    expect(screen.getByTestId("mode").textContent).toBe("system");
    expect(localStorage.getItem("md_theme")).toBe("system");
  });

  it("restores a saved choice and ignores system changes after an explicit one", () => {
    localStorage.setItem("md_theme", "dark");
    mount();
    expect(isDark()).toBe(true);
    act(() => listeners.forEach((l) => l({ matches: false })));
    expect(isDark()).toBe(true);
  });

  it("ignores a corrupt saved value", () => {
    localStorage.setItem("md_theme", "purple");
    mount();
    expect(screen.getByTestId("mode").textContent).toBe("system");
  });

  it("still switches theme when storage is blocked (the choice just does not persist)", () => {
    vi.spyOn(localStorage, "setItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    vi.spyOn(localStorage, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    mount();
    expect(screen.getByTestId("mode").textContent).toBe("system");
    act(() => screen.getByText("dark").click());
    expect(isDark()).toBe(true);
    expect(screen.getByTestId("mode").textContent).toBe("dark");
  });

  it("sets the browser colour scheme so form controls and scrollbars match", () => {
    mount();
    act(() => screen.getByText("dark").click());
    expect(document.documentElement.style.colorScheme).toBe("dark");
  });
});
