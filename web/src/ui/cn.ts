/** Join class names, skipping anything falsy. (A tiny stand-in for clsx; no dependency needed.) */
export function cn(...parts: (string | false | null | undefined)[]): string {
  return parts.filter(Boolean).join(" ");
}
