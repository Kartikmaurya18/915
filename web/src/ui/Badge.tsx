import type { ReactNode } from "react";
import { ArrowDownRight, ArrowUpRight, Minus } from "lucide-react";
import { fmt } from "../lib/format";
import { cn } from "./cn";

type Tone = "neutral" | "brand" | "pos" | "neg" | "warn";

const TONE: Record<Tone, string> = {
  neutral: "bg-surface-2 text-muted",
  brand: "bg-brand-soft text-brand-fg",
  pos: "bg-pos-soft text-pos",
  neg: "bg-neg-soft text-neg",
  warn: "bg-warn-soft text-warn",
};

export function Badge({ tone = "neutral", children, className }: { tone?: Tone; children: ReactNode; className?: string }) {
  return <span className={cn("inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium", TONE[tone], className)}>{children}</span>;
}

/**
 * A gain or loss. Direction is carried by an arrow AND a sign AND a screen-reader word, never by colour alone.
 */
export function DeltaBadge({ value, percent = false, digits = 2, className }: { value: number | null | undefined; percent?: boolean; digits?: number; className?: string }) {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return <Badge className={className}>—</Badge>;
  }
  const direction = value > 0 ? "up" : value < 0 ? "down" : "flat";
  const Icon = direction === "up" ? ArrowUpRight : direction === "down" ? ArrowDownRight : Minus;
  const sign = value > 0 ? "+" : "";
  return (
    <Badge tone={direction === "up" ? "pos" : direction === "down" ? "neg" : "neutral"} className={cn("num", className)}>
      <Icon aria-hidden="true" className="size-3.5" />
      <span className="sr-only">{direction === "up" ? "Up " : direction === "down" ? "Down " : "Unchanged "}</span>
      {sign}
      {fmt(value, digits)}
      {percent ? "%" : ""}
    </Badge>
  );
}

/** A figure coloured by sign, with an explicit sign so colour is never the only cue. */
export function Signed({ value, digits = 2, className }: { value: number | null | undefined; digits?: number; className?: string }) {
  if (value === null || value === undefined || Number.isNaN(value)) return <span className={cn("num text-muted", className)}>—</span>;
  const tone = value > 0 ? "text-pos" : value < 0 ? "text-neg" : "text-fg";
  return (
    <span className={cn("num", tone, className)}>
      {value > 0 ? "+" : ""}
      {fmt(value, digits)}
    </span>
  );
}
