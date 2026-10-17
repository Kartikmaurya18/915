import { forwardRef, useId, type InputHTMLAttributes, type ReactNode, type SelectHTMLAttributes } from "react";
import { cn } from "./cn";

const CONTROL =
  "min-h-10 rounded-control border border-control bg-surface px-3 text-sm text-fg placeholder:text-subtle " +
  "transition-colors hover:border-fg/40 disabled:cursor-not-allowed disabled:opacity-60 aria-[invalid=true]:border-neg";

interface FieldProps {
  label: ReactNode;
  hint?: ReactNode;
  error?: ReactNode;
  className?: string;
}

/** A label, a control and its hint/error, wired together for assistive technology. */
export function Field({ label, hint, error, className, children }: FieldProps & { children: (props: { id: string; "aria-describedby"?: string; "aria-invalid"?: true }) => ReactNode }) {
  const id = useId();
  // The hint is hidden while there is an error, so only reference the element that is actually rendered.
  const describedBy = error ? id + "-error" : hint ? id + "-hint" : undefined;
  return (
    <div className={cn("flex min-w-0 flex-col gap-1.5", className)}>
      <label htmlFor={id} className="text-sm font-medium text-fg">
        {label}
      </label>
      {children({ id, "aria-describedby": describedBy, "aria-invalid": error ? true : undefined })}
      {hint && !error && (
        <p id={id + "-hint"} className="text-xs text-muted">
          {hint}
        </p>
      )}
      {error && (
        <p id={id + "-error"} role="alert" className="text-xs font-medium text-neg">
          {error}
        </p>
      )}
    </div>
  );
}

/** Full width unless the caller sets its own width (two competing width classes would be decided by stylesheet order). */
const fullWidthUnless = (className?: string) => !/(^|\s)w-/.test(className ?? "") && "w-full";

export const Input = forwardRef<HTMLInputElement, InputHTMLAttributes<HTMLInputElement>>(function Input({ className, ...rest }, ref) {
  return <input ref={ref} className={cn(CONTROL, fullWidthUnless(className), "num", className)} {...rest} />;
});

export const Select = forwardRef<HTMLSelectElement, SelectHTMLAttributes<HTMLSelectElement>>(function Select({ className, children, ...rest }, ref) {
  return (
    <select ref={ref} className={cn(CONTROL, fullWidthUnless(className), className)} {...rest}>
      {children}
    </select>
  );
});

/** A checkbox with a comfortable hit area. Pass an aria-label when there is no visible label. */
export const Checkbox = forwardRef<HTMLInputElement, InputHTMLAttributes<HTMLInputElement>>(function Checkbox({ className, ...rest }, ref) {
  return <input ref={ref} type="checkbox" className={cn("size-4 cursor-pointer rounded border-control accent-[var(--brand)]", className)} {...rest} />;
});

export function Kbd({ children }: { children: ReactNode }) {
  return <kbd className="num rounded border border-line bg-surface-2 px-1.5 py-0.5 font-sans text-[11px] font-medium text-muted">{children}</kbd>;
}
