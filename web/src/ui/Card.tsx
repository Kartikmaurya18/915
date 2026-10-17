import { useId, type HTMLAttributes, type ReactNode } from "react";
import { cn } from "./cn";

interface CardProps extends Omit<HTMLAttributes<HTMLElement>, "title"> {
  /** Visible heading. It also becomes the section's accessible name. */
  title?: ReactNode;
  description?: ReactNode;
  /** Controls shown at the right of the header (tabs, buttons). */
  actions?: ReactNode;
  /** Remove the inner padding (tables that run edge to edge). */
  flush?: boolean;
}

export function Card({ title, description, actions, flush = false, className, children, ...rest }: CardProps) {
  const headingId = useId();
  return (
    <section aria-labelledby={title ? headingId : undefined} className={cn("min-w-0 rounded-card border border-line bg-surface shadow-card", className)} {...rest}>
      {(title || actions) && (
        <header className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2 px-4 pt-4 sm:px-5">
          <div className="min-w-0">
            {title && (
              <h2 id={headingId} className="text-base font-semibold tracking-tight text-fg">
                {title}
              </h2>
            )}
            {description && <p className="mt-0.5 text-sm text-muted">{description}</p>}
          </div>
          {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
        </header>
      )}
      <div className={cn(flush ? "pt-3" : "p-4 sm:p-5", Boolean(title || actions) && !flush && "pt-3 sm:pt-3")}>{children}</div>
    </section>
  );
}
