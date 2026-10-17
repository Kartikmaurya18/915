import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { SHORTCUTS, interpret, isEditable } from "../lib/shortcuts";
import { Dialog } from "../ui/Dialog";
import { useUI } from "../ui/UIProvider";

const GO_WINDOW_MS = 1200;

/** Global single-key shortcuts and the dialog that lists them. Rendered once, inside the router. */
export function Shortcuts() {
  const navigate = useNavigate();
  const { openOrder, openPalette } = useUI();
  const [help, setHelp] = useState(false);
  const armed = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    const disarm = () => {
      if (armed.current) clearTimeout(armed.current);
      armed.current = null;
    };
    const onKey = (e: KeyboardEvent) => {
      const action = interpret({
        key: e.key,
        ctrlKey: e.ctrlKey,
        metaKey: e.metaKey,
        altKey: e.altKey,
        editable: isEditable(e.target),
        modalOpen: document.querySelector("dialog[open]") !== null,
        goArmed: armed.current !== null,
      });
      if (!action) {
        if (armed.current && e.key !== "Shift") disarm(); // any other key cancels a pending "g"
        return;
      }
      e.preventDefault();
      disarm();
      if (action.type === "arm-go") armed.current = setTimeout(disarm, GO_WINDOW_MS);
      else if (action.type === "go") navigate(action.to);
      else if (action.type === "order") openOrder();
      else if (action.type === "palette") openPalette();
      else if (action.type === "help") setHelp(true);
    };
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
      disarm();
    };
  }, [navigate, openOrder, openPalette]);

  return (
    <Dialog open={help} onClose={() => setHelp(false)} label="Keyboard shortcuts" placement="palette">
      <div className="p-5">
        <h2 className="text-lg font-semibold tracking-tight">Keyboard shortcuts</h2>
        <p className="mt-1 text-sm text-muted">Single keys work anywhere except while you are typing in a field.</p>
        <dl className="mt-4 grid gap-1.5">
          {SHORTCUTS.map((s) => (
            <div key={s.keys + s.label} className="flex items-center justify-between gap-4 border-b border-line/70 py-1.5 text-sm last:border-0">
              <dt className="text-muted">{s.label}</dt>
              <dd><kbd className="num rounded border border-control bg-surface-2 px-1.5 py-0.5 text-xs">{s.keys}</kbd></dd>
            </div>
          ))}
        </dl>
      </div>
    </Dialog>
  );
}
