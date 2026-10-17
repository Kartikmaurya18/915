import { useEffect, useId, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import { CalendarDays, CornerDownLeft, FileText, LogOut, Palette, Plus, Search, Trash2, TrendingUp, type LucideIcon } from "lucide-react";
import type { OptionsTopic } from "../api/types";
import { useConfig } from "../api/useConfig";
import { useAuth } from "../auth/AuthContext";
import { fmt, fmtInt } from "../lib/format";
import { NAV } from "../nav";
import { useTopic } from "../realtime/RealtimeProvider";
import { useSelection } from "../state/SelectionContext";
import { useTheme } from "../theme/ThemeProvider";
import { Dialog } from "../ui/Dialog";
import { Kbd } from "../ui/fields";
import { cn } from "../ui/cn";
import { useUI } from "../ui/UIProvider";

type Group = "Contracts" | "Pages" | "Actions" | "Expiry";
const GROUP_ORDER: Group[] = ["Contracts", "Pages", "Actions", "Expiry"];

interface Command {
  id: string;
  group: Group;
  label: string;
  /** Extra text that is searchable and shown at the right. */
  detail?: string;
  /** Words that also match this command. */
  keywords?: string;
  icon: LucideIcon;
  run: () => void;
}

/** Every word the user typed must appear somewhere in the command (commas are ignored, so "24800" finds "24,800"). */
export function matches(command: Pick<Command, "label" | "detail" | "keywords" | "group">, query: string): boolean {
  const haystack = `${command.label} ${command.detail ?? ""} ${command.keywords ?? ""} ${command.group}`.toLowerCase().replace(/,/g, "");
  return query
    .toLowerCase()
    .replace(/,/g, "")
    .split(/\s+/)
    .filter(Boolean)
    .every((word) => haystack.includes(word));
}

export default function CommandPalette() {
  const { paletteOpen, closePalette } = useUI();
  return (
    <Dialog open={paletteOpen} onClose={closePalette} label="Search and commands" placement="palette">
      <PaletteBody />
    </Dialog>
  );
}

function PaletteBody() {
  const ui = useUI();
  const navigate = useNavigate();
  const { user, mode, logout } = useAuth();
  const { cycle, mode: themeMode } = useTheme();
  const selection = useSelection();
  const { config } = useConfig();
  const options = useTopic<OptionsTopic>(config ? `options:${config.underlying}` : null).data;
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const listId = useId();
  const inputRef = useRef<HTMLInputElement>(null);
  const isAdmin = user?.role === "admin";

  const commands = useMemo<Command[]>(() => {
    const go = (path: string) => () => {
      ui.closePalette();
      navigate(path);
    };
    const out: Command[] = [];

    // Contracts: every strike in the live chain, once as CE and once as PE.
    for (const row of options?.strikes ?? []) {
      for (const type of ["CE", "PE"] as const) {
        const price = type === "CE" ? row.call_ltp : row.put_ltp;
        out.push({
          id: `contract-${row.strike}-${type}`,
          group: "Contracts",
          label: `${fmtInt(row.strike)} ${type}${row.is_atm ? " (at the money)" : ""}`,
          detail: price === null ? undefined : fmt(price),
          keywords: `trade buy sell strike ${row.strike} ${type === "CE" ? "call" : "put"}`,
          icon: TrendingUp,
          run: () => ui.openOrder({ optionType: type, strike: row.strike }),
        });
      }
    }
    for (const item of NAV.filter((n) => !n.admin || isAdmin)) {
      out.push({ id: `page-${item.to}`, group: "Pages", label: item.label, keywords: "go open page", icon: item.icon, run: go(item.to) });
    }
    out.push({ id: "act-order", group: "Actions", label: "New simulated order", keywords: "buy sell trade execute order", icon: Plus, run: () => ui.openOrder() });
    out.push({ id: "act-theme", group: "Actions", label: "Change theme", detail: themeMode, keywords: "dark light mode appearance system", icon: Palette, run: () => cycle() });
    if (selection.selection.size > 0) {
      out.push({ id: "act-review", group: "Actions", label: `Review ${selection.selection.size} selected contract${selection.selection.size === 1 ? "" : "s"}`, keywords: "selected trade", icon: FileText, run: go("/trading") });
      out.push({ id: "act-clear", group: "Actions", label: "Clear selected contracts", keywords: "remove deselect", icon: Trash2, run: () => { selection.clear(); ui.closePalette(); } });
    }
    if (mode === "required") {
      out.push({
        id: "act-signout",
        group: "Actions",
        label: "Sign out",
        keywords: "log out logout",
        icon: LogOut,
        run: async () => {
          ui.closePalette();
          await logout();
          navigate("/login", { replace: true });
        },
      });
    }
    if (options?.expiry) {
      out.push({ id: "expiry-current", group: "Expiry", label: `Expiry ${options.expiry}`, detail: "current option chain", keywords: "expiry expiries chain date", icon: CalendarDays, run: go("/#option-chain") });
    }
    return out;
  }, [options, isAdmin, mode, themeMode, selection, ui, navigate, cycle, logout]);

  const results = useMemo(() => {
    const trimmed = query.trim();
    const found = commands.filter((c) => matches(c, trimmed));
    // With no query show the useful short list, not every strike.
    const limited = trimmed ? found : found.filter((c) => c.group !== "Contracts").concat(found.filter((c) => c.group === "Contracts" && c.label.includes("at the money")));
    const capped: Command[] = [];
    const count: Partial<Record<Group, number>> = {};
    for (const group of GROUP_ORDER) {
      for (const c of limited.filter((x) => x.group === group)) {
        count[group] = (count[group] ?? 0) + 1;
        if (count[group]! <= (group === "Contracts" ? 8 : 12)) capped.push(c);
      }
    }
    return capped;
  }, [commands, query]);

  useEffect(() => setActive(0), [query]);
  useEffect(() => {
    document.getElementById(`${listId}-opt-${active}`)?.scrollIntoView({ block: "nearest" });
  }, [active, listId]);

  function onKeyDown(e: KeyboardEvent<HTMLInputElement>) {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setActive((a) => (results.length ? (a + 1) % results.length : 0));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActive((a) => (results.length ? (a - 1 + results.length) % results.length : 0));
    } else if (e.key === "Home") {
      e.preventDefault();
      setActive(0);
    } else if (e.key === "End") {
      e.preventDefault();
      setActive(Math.max(0, results.length - 1));
    } else if (e.key === "Enter") {
      e.preventDefault();
      results[active]?.run();
    }
  }

  // Group headers, in order, with the global index of each option.
  const sections: { group: Group; items: { command: Command; index: number }[] }[] = [];
  results.forEach((command, index) => {
    let section = sections.find((s) => s.group === command.group);
    if (!section) sections.push((section = { group: command.group, items: [] }));
    section.items.push({ command, index });
  });

  const activeId = results.length ? `${listId}-opt-${active}` : undefined;
  return (
    <div className="flex max-h-[min(34rem,80dvh)] flex-col">
      <div className="flex items-center gap-2 border-b border-line px-3">
        <Search aria-hidden="true" className="size-4 shrink-0 text-muted" />
        <input
          ref={inputRef}
          autoFocus
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={onKeyDown}
          role="combobox"
          aria-expanded="true"
          aria-controls={listId}
          aria-activedescendant={activeId}
          aria-autocomplete="list"
          aria-label="Search strikes, pages and actions"
          placeholder="Search strikes, pages, actions…"
          autoComplete="off"
          spellCheck={false}
          className="num min-h-12 w-full bg-transparent text-base text-fg outline-none placeholder:text-subtle focus-visible:outline-none"
        />
        <Kbd>Esc</Kbd>
      </div>

      <div role="status" aria-live="polite" className="sr-only">
        {results.length === 0 ? "No results" : `${results.length} result${results.length === 1 ? "" : "s"}`}
      </div>

      <ul id={listId} role="listbox" aria-label="Results" className="overflow-y-auto p-2">
        {results.length === 0 && <li role="presentation" className="px-3 py-8 text-center text-sm text-muted">Nothing matches “{query}”. Try a strike like 24800, or a page name.</li>}
        {sections.map((section) => (
          <li key={section.group} role="presentation">
            <ul role="group" aria-label={section.group}>
              <li role="presentation" aria-hidden="true" className="px-3 pb-1 pt-2 text-[11px] font-semibold uppercase tracking-wide text-subtle">
                {section.group}
              </li>
              {section.items.map(({ command, index }) => (
                <Option key={command.id} id={`${listId}-opt-${index}`} command={command} selected={index === active} onHover={() => setActive(index)} />
              ))}
            </ul>
          </li>
        ))}
      </ul>

      <div className="flex items-center gap-3 border-t border-line px-3 py-2 text-xs text-muted">
        <span className="flex items-center gap-1"><Kbd>↑</Kbd><Kbd>↓</Kbd> to move</span>
        <span className="flex items-center gap-1"><Kbd><CornerDownLeft aria-label="Enter" className="size-3" /></Kbd> to run</span>
        <span className="ml-auto hidden sm:inline">Simulated trading only</span>
      </div>
    </div>
  );
}

function Option({ id, command, selected, onHover }: { id: string; command: Command; selected: boolean; onHover: () => void }): ReactNode {
  const Icon = command.icon;
  return (
    <li
      id={id}
      role="option"
      aria-selected={selected}
      onMouseMove={onHover}
      onClick={command.run}
      className={cn("flex min-h-11 cursor-pointer items-center gap-3 rounded-control px-3 text-sm", selected ? "bg-brand-soft text-fg" : "text-fg")}
    >
      <Icon aria-hidden="true" className={cn("size-4 shrink-0", selected ? "text-brand-fg" : "text-muted")} />
      <span className="min-w-0 flex-1 truncate">{command.label}</span>
      {command.detail && <span className="num shrink-0 text-xs text-muted">{command.detail}</span>}
    </li>
  );
}
