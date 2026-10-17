import type { ReactNode } from "react";
import { AlertTriangle, Inbox } from "lucide-react";
import { cn } from "./cn";

/** A placeholder the shape of the content that is loading. Decorative: the region carries aria-busy instead. */
export function Skeleton({ className }: { className?: string }) {
  return (
    <span
      aria-hidden="true"
      className={cn(
        "block rounded-control bg-[linear-gradient(90deg,var(--surface-2)_25%,var(--line)_37%,var(--surface-2)_63%)] bg-[length:200%_100%] motion-safe:animate-[shimmer_1.4s_ease-in-out_infinite]",
        className,
      )}
    />
  );
}

/** Skeletons for a row of metric cards. */
export function MetricSkeletons({ count = 4 }: { count?: number }) {
  return (
    <div aria-busy="true" aria-label="Loading figures" role="status" className="grid grid-cols-2 gap-3 lg:grid-cols-4">
      {Array.from({ length: count }, (_, i) => (
        <div key={i} className="rounded-card border border-line bg-surface p-4 shadow-card">
          <Skeleton className="h-3 w-20" />
          <Skeleton className="mt-3 h-7 w-28" />
          <Skeleton className="mt-3 h-3 w-24" />
        </div>
      ))}
    </div>
  );
}

export function EmptyState({ title, children, icon, className }: { title: string; children?: ReactNode; icon?: ReactNode; className?: string }) {
  return (
    <div className={cn("flex flex-col items-center gap-1.5 px-4 py-8 text-center", className)}>
      <span aria-hidden="true" className="mb-1 grid size-10 place-items-center rounded-full bg-surface-2 text-muted">
        {icon ?? <Inbox className="size-5" />}
      </span>
      <p className="font-medium text-fg">{title}</p>
      {children && <p className="max-w-sm text-sm text-muted">{children}</p>}
    </div>
  );
}

/** An error the user can read and act on. Announced to screen readers as it appears. */
export function ErrorState({ children, action, className }: { children: ReactNode; action?: ReactNode; className?: string }) {
  return (
    <div role="alert" className={cn("flex items-start gap-3 rounded-control border border-neg/30 bg-neg-soft p-3 text-sm text-neg", className)}>
      <AlertTriangle aria-hidden="true" className="mt-0.5 size-4 shrink-0" />
      <div className="min-w-0 flex-1">{children}</div>
      {action}
    </div>
  );
}

/** Inline, non-blocking success or error text under a form. */
export function FormMessage({ ok, children }: { ok: boolean; children: ReactNode }) {
  return (
    <p role={ok ? "status" : "alert"} className={cn("text-sm", ok ? "text-pos" : "text-neg")}>
      {children}
    </p>
  );
}
