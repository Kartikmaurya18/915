import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";

export type ThemeMode = "light" | "dark" | "system";
export type ResolvedTheme = "light" | "dark";

const KEY = "md_theme";
const ORDER: ThemeMode[] = ["system", "light", "dark"];

interface ThemeApi {
  /** What the user chose: follow the system, or force light or dark. */
  mode: ThemeMode;
  /** What is actually showing. */
  resolved: ResolvedTheme;
  setMode(mode: ThemeMode): void;
  /** system -> light -> dark -> system */
  cycle(): void;
}

const Ctx = createContext<ThemeApi | null>(null);

function readSaved(): ThemeMode {
  try {
    const v = localStorage.getItem(KEY);
    if (v === "light" || v === "dark" || v === "system") return v;
  } catch {
    /* storage blocked (private window, strict settings): the choice then lasts for this visit only */
  }
  return "system";
}

function systemPrefersDark(): boolean {
  return typeof window !== "undefined" && typeof window.matchMedia === "function" && window.matchMedia("(prefers-color-scheme: dark)").matches;
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [mode, setModeState] = useState<ThemeMode>(readSaved);
  const [systemDark, setSystemDark] = useState<boolean>(systemPrefersDark);

  // Track the operating system while the user has not chosen explicitly.
  useEffect(() => {
    if (typeof window.matchMedia !== "function") return;
    const query = window.matchMedia("(prefers-color-scheme: dark)");
    const onChange = (e: MediaQueryListEvent) => setSystemDark(e.matches);
    query.addEventListener("change", onChange);
    return () => query.removeEventListener("change", onChange);
  }, []);

  const resolved: ResolvedTheme = mode === "system" ? (systemDark ? "dark" : "light") : mode;

  useEffect(() => {
    const root = document.documentElement;
    root.classList.toggle("dark", resolved === "dark");
    root.style.colorScheme = resolved;
  }, [resolved]);

  const setMode = useCallback((next: ThemeMode) => {
    setModeState(next);
    try {
      localStorage.setItem(KEY, next);
    } catch {
      /* see readSaved */
    }
  }, []);

  const cycle = useCallback(() => setMode(ORDER[(ORDER.indexOf(mode) + 1) % ORDER.length]!), [mode, setMode]);

  const value = useMemo(() => ({ mode, resolved, setMode, cycle }), [mode, resolved, setMode, cycle]);
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useTheme(): ThemeApi {
  const v = useContext(Ctx);
  if (!v) throw new Error("useTheme must be used inside <ThemeProvider>");
  return v;
}
