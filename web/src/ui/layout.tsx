import type { ReactNode } from "react";
import { DeltaBadge } from "./Badge";
import { Skeleton } from "./feedback";
import { cn } from "./cn";

/** Page title row. Every page has exactly one h1. */
export function PageHeader({ title, description, actions }: { title: string; description?: ReactNode; actions?: ReactNode }) {
  return (
    <div className="flex flex-wrap items-end justify-between gap-x-4 gap-y-2">
      <div className="min-w-0">
        <h1 className="text-2xl font-semibold tracking-tight text-fg">{title}</h1>
        {description && <p className="mt-0.5 text-sm text-muted">{description}</p>}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </div>
  );
}

interface MetricProps {
  label: string;
  value: ReactNode;
  sub?: ReactNode;
  /** A gain or loss shown as a badge next to the value. */
  delta?: number | null;
  deltaPercent?: boolean;
  /** Skeleton instead of the value while the first data loads. */
  loading?: boolean;
  tone?: "pos" | "neg" | "neutral";
}

/** A summary card: small caption, a large figure, optional delta badge and sub-line. */
export function Metric({ label, value, sub, delta, deltaPercent, loading, tone = "neutral" }: MetricProps) {
  return (
    <div className="min-w-0 rounded-card border border-line bg-surface p-4 shadow-card">
      <p className="text-xs font-medium uppercase tracking-wide text-subtle">{label}</p>
      <div className="mt-1.5 flex flex-wrap items-baseline gap-x-2 gap-y-1">
        {loading ? (
          <Skeleton className="h-8 w-28" />
        ) : (
          <p className={cn("num text-2xl font-semibold tracking-tight", tone === "pos" && "text-pos", tone === "neg" && "text-neg")}>{value}</p>
        )}
        {delta !== undefined && !loading && <DeltaBadge value={delta} percent={deltaPercent} />}
      </div>
      {sub && <p className="num mt-1 text-xs wrap-break-word text-muted">{loading ? <Skeleton className="h-3 w-24" /> : sub}</p>}
    </div>
  );
}

export function MetricGrid({ children, columns = 4 }: { children: ReactNode; columns?: 2 | 3 | 4 }) {
  return <div className={cn("grid grid-cols-2 gap-3", columns === 4 && "lg:grid-cols-4", columns === 3 && "lg:grid-cols-3")}>{children}</div>;
}

// -- tables ----------------------------------------------------------------------------------------------------

/** Horizontal scroll container with a keyboard-focusable region, so wide tables can be scrolled without a mouse. */
export function TableWrap({ children, label, className }: { children: ReactNode; label: string; className?: string }) {
  return (
    <div role="region" aria-label={label} tabIndex={0} className={cn("overflow-auto rounded-control", className)}>
      {children}
    </div>
  );
}

export const TABLE = "w-full border-separate border-spacing-0 text-sm";
export const TH = "sticky top-0 z-10 whitespace-nowrap border-b border-line bg-surface px-3 py-2 text-left text-xs font-medium uppercase tracking-wide text-subtle";
export const TD = "num whitespace-nowrap border-b border-line/70 px-3 py-2";
export const R = "text-right";
