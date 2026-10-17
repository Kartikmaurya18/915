/** Keyboard shortcuts. The decision of what a key press means is pure, so it is tested without a browser. */

export interface Shortcut {
  keys: string;
  label: string;
}

export const GO_TARGETS: Record<string, { to: string; label: string }> = {
  o: { to: "/", label: "Overview" },
  t: { to: "/trading", label: "Paper trading" },
  s: { to: "/strategy", label: "Strategy builder" },
  w: { to: "/workspace", label: "Workspace" },
  h: { to: "/history", label: "History" },
  r: { to: "/rrg", label: "Relative rotation" },
  q: { to: "/quality", label: "Data quality" },
};

export const SHORTCUTS: readonly Shortcut[] = [
  { keys: "Ctrl K", label: "Search strikes and pages" },
  { keys: "/", label: "Search strikes and pages" },
  { keys: "N", label: "New simulated order" },
  ...Object.entries(GO_TARGETS).map(([k, v]) => ({ keys: `G then ${k.toUpperCase()}`, label: `Go to ${v.label}` })),
  { keys: "?", label: "Show this list" },
  { keys: "Esc", label: "Close a panel" },
];

export type Action = { type: "palette" } | { type: "order" } | { type: "help" } | { type: "go"; to: string } | { type: "arm-go" };

export interface KeyInfo {
  key: string;
  ctrlKey: boolean;
  metaKey: boolean;
  altKey: boolean;
  /** The element that has focus. */
  editable: boolean;
  /** A modal is open: the page behind it must not react. */
  modalOpen: boolean;
  /** The user pressed G a moment ago. */
  goArmed: boolean;
}

/** What a key press should do, or null to let it through untouched. */
export function interpret(k: KeyInfo): Action | null {
  if (k.ctrlKey || k.metaKey || k.altKey || k.editable || k.modalOpen) return null;
  if (k.goArmed) {
    const target = GO_TARGETS[k.key.toLowerCase()];
    return target ? { type: "go", to: target.to } : null;
  }
  switch (k.key) {
    case "/":
      return { type: "palette" };
    case "n":
    case "N":
      return { type: "order" };
    case "?":
      return { type: "help" };
    case "g":
    case "G":
      return { type: "arm-go" };
    default:
      return null;
  }
}

export function isEditable(el: EventTarget | null): boolean {
  if (!(el instanceof HTMLElement)) return false;
  return el.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(el.tagName) || el.getAttribute("role") === "combobox";
}
