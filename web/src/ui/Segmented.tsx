import { useRef, type KeyboardEvent } from "react";
import { cn } from "./cn";

export interface SegmentOption<T extends string> {
  value: T;
  label: string;
  /** Longer name for screen readers when the visible label is terse ("1m" -> "1 minute"). */
  title?: string;
}

interface SegmentedProps<T extends string> {
  options: readonly SegmentOption<T>[];
  value: T;
  onChange: (value: T) => void;
  /** Accessible name of the group. */
  label: string;
  size?: "sm" | "md";
  className?: string;
}

/**
 * Tabs in a segmented control. Implements the WAI-ARIA tabs pattern: one tab stop, arrow keys / Home / End
 * move and select, so it is fully operable from the keyboard.
 */
export function Segmented<T extends string>({ options, value, onChange, label, size = "md", className }: SegmentedProps<T>) {
  const refs = useRef<(HTMLButtonElement | null)[]>([]);

  function onKeyDown(e: KeyboardEvent<HTMLDivElement>) {
    const current = options.findIndex((o) => o.value === value);
    let next = current;
    if (e.key === "ArrowRight" || e.key === "ArrowDown") next = (current + 1) % options.length;
    else if (e.key === "ArrowLeft" || e.key === "ArrowUp") next = (current - 1 + options.length) % options.length;
    else if (e.key === "Home") next = 0;
    else if (e.key === "End") next = options.length - 1;
    else return;
    e.preventDefault();
    onChange(options[next]!.value);
    refs.current[next]?.focus();
  }

  return (
    <div role="tablist" aria-label={label} onKeyDown={onKeyDown} className={cn("inline-flex rounded-control border border-control bg-surface p-0.5", className)}>
      {options.map((o, i) => {
        const selected = o.value === value;
        return (
          <button
            key={o.value}
            ref={(el) => {
              refs.current[i] = el;
            }}
            role="tab"
            type="button"
            aria-selected={selected}
            tabIndex={selected ? 0 : -1}
            onClick={() => onChange(o.value)}
            className={cn(
              "num rounded-[6px] font-medium transition-colors",
              size === "sm" ? "min-h-7 px-2.5 text-xs" : "min-h-8 px-3 text-sm",
              selected ? "bg-brand text-on-brand shadow-card" : "text-muted hover:bg-surface-2 hover:text-fg",
            )}
          >
            {o.label}
            {o.title && <span className="sr-only"> ({o.title})</span>}
          </button>
        );
      })}
    </div>
  );
}
