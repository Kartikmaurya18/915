/** Workspace layout: how many chart panes are shown and what each one displays. Pure, so it is testable and safe to restore. */

export type PaneKind = "price" | "straddle" | "strangle";
export type LayoutId = "1" | "2" | "4";

export const PANE_KINDS: readonly { value: PaneKind; label: string }[] = [
  { value: "price", label: "Price candles" },
  { value: "straddle", label: "Straddle at a strike" },
  { value: "strangle", label: "Strangle (CE + PE)" },
];

export const LAYOUTS: readonly { value: LayoutId; label: string; panes: number; title: string }[] = [
  { value: "1", label: "1", panes: 1, title: "One chart" },
  { value: "2", label: "2", panes: 2, title: "Two charts side by side" },
  { value: "4", label: "4", panes: 4, title: "Four charts in a grid" },
];

export const MAX_PANES = 4;

export interface WorkspaceState {
  layout: LayoutId;
  /** Always MAX_PANES long, so switching to a bigger layout brings back what each pane was showing. */
  panes: PaneKind[];
}

export const DEFAULT_WORKSPACE: WorkspaceState = { layout: "2", panes: ["price", "straddle", "strangle", "price"] };

const KINDS = new Set<string>(PANE_KINDS.map((k) => k.value));
const LAYOUT_IDS = new Set<string>(LAYOUTS.map((l) => l.value));

export function parseWorkspace(raw: unknown): WorkspaceState {
  if (!raw || typeof raw !== "object") return DEFAULT_WORKSPACE;
  const { layout, panes } = raw as { layout?: unknown; panes?: unknown };
  const picked = Array.isArray(panes) ? panes.filter((p): p is PaneKind => typeof p === "string" && KINDS.has(p)).slice(0, MAX_PANES) : [];
  while (picked.length < MAX_PANES) picked.push(DEFAULT_WORKSPACE.panes[picked.length]!);
  return { layout: typeof layout === "string" && LAYOUT_IDS.has(layout) ? (layout as LayoutId) : DEFAULT_WORKSPACE.layout, panes: picked };
}

export const paneCount = (layout: LayoutId): number => LAYOUTS.find((l) => l.value === layout)!.panes;

/** Tailwind grid classes: one column on a phone, the chosen layout from the `lg` breakpoint up. */
export const gridClass = (layout: LayoutId): string => (layout === "1" ? "grid-cols-1" : "grid-cols-1 lg:grid-cols-2");

const KEY = "md_workspace";
export function loadWorkspace(): WorkspaceState {
  try {
    const raw = localStorage.getItem(KEY);
    return raw ? parseWorkspace(JSON.parse(raw)) : DEFAULT_WORKSPACE;
  } catch {
    return DEFAULT_WORKSPACE;
  }
}
export function saveWorkspace(w: WorkspaceState): void {
  try {
    localStorage.setItem(KEY, JSON.stringify(w));
  } catch {
    /* a preference only */
  }
}
