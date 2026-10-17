import { useEffect, useRef, type ReactNode } from "react";
import { cn } from "./cn";

interface DialogProps {
  open: boolean;
  onClose: () => void;
  /** Accessible name of the dialog. */
  label: string;
  /** `drawer`: slides in from the right, becomes a bottom sheet on phones. `palette`: a top-centred search box. */
  placement: "drawer" | "palette";
  children: ReactNode;
}

const PLACEMENT = {
  drawer:
    "m-0 ml-auto mt-auto sm:mt-0 h-auto max-h-[92dvh] w-full rounded-t-card sm:h-dvh sm:max-h-none sm:w-[26rem] sm:rounded-none sm:rounded-l-card " +
    "animate-[slide-in-up_0.22s_ease-out] sm:animate-[slide-in-right_0.22s_ease-out]",
  palette: "mx-auto mt-[8vh] w-[min(40rem,calc(100vw-1.5rem))] rounded-card animate-[fade-in_0.12s_ease-out]",
} as const;

/**
 * A modal built on the native <dialog> element. The browser provides what is hard to get right by hand:
 * the rest of the page becomes inert, focus is trapped inside, Escape closes it, and focus returns to the
 * control that opened it. We only add backdrop-click to close and keep React's `open` state in sync.
 * Children are mounted only while open, so forms start fresh each time.
 */
export function Dialog({ open, onClose, label, placement, children }: DialogProps) {
  const ref = useRef<HTMLDialogElement>(null);

  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    if (open && !dialog.open) dialog.showModal();
    if (!open && dialog.open) dialog.close();
  }, [open]);

  return (
    <dialog
      ref={ref}
      aria-label={label}
      onCancel={(e) => {
        e.preventDefault(); // let React own the state, then close through it
        onClose();
      }}
      onClose={() => {
        if (open) onClose(); // closed by the browser some other way
      }}
      onClick={(e) => {
        if (e.target === ref.current) onClose(); // a click on the backdrop, not on the content
      }}
      className={cn("overflow-hidden border border-line bg-surface p-0 text-fg shadow-pop", PLACEMENT[placement])}
    >
      {open && children}
    </dialog>
  );
}
