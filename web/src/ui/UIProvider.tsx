import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";

export interface OrderPreset {
  optionType?: "CE" | "PE";
  strike?: number | null;
  side?: "BUY" | "SELL";
}

interface UIApi {
  paletteOpen: boolean;
  openPalette(): void;
  closePalette(): void;
  orderOpen: boolean;
  orderPreset: OrderPreset;
  openOrder(preset?: OrderPreset): void;
  closeOrder(): void;
  /** True once the palette / drawer has been opened, so their code is only loaded on first use. */
  paletteLoaded: boolean;
  orderLoaded: boolean;
}

const Ctx = createContext<UIApi | null>(null);

/** Owns the global overlays and the Ctrl/Cmd+K shortcut. */
export function UIProvider({ children }: { children: ReactNode }) {
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [paletteLoaded, setPaletteLoaded] = useState(false);
  const [orderOpen, setOrderOpen] = useState(false);
  const [orderLoaded, setOrderLoaded] = useState(false);
  const [orderPreset, setOrderPreset] = useState<OrderPreset>({});

  const openPalette = useCallback(() => {
    setPaletteLoaded(true);
    setPaletteOpen(true);
  }, []);
  const closePalette = useCallback(() => setPaletteOpen(false), []);
  const openOrder = useCallback((preset: OrderPreset = {}) => {
    setOrderPreset(preset);
    setOrderLoaded(true);
    setOrderOpen(true);
    setPaletteOpen(false);
  }, []);
  const closeOrder = useCallback(() => setOrderOpen(false), []);

  // Ctrl+K on Windows/Linux, Cmd+K on macOS: toggles the palette from anywhere.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && !e.altKey && !e.shiftKey && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setPaletteLoaded(true);
        setPaletteOpen((open) => !open);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const value = useMemo<UIApi>(
    () => ({ paletteOpen, openPalette, closePalette, orderOpen, orderPreset, openOrder, closeOrder, paletteLoaded, orderLoaded }),
    [paletteOpen, openPalette, closePalette, orderOpen, orderPreset, openOrder, closeOrder, paletteLoaded, orderLoaded],
  );
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useUI(): UIApi {
  const v = useContext(Ctx);
  if (!v) throw new Error("useUI must be used inside <UIProvider>");
  return v;
}
