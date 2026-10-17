import { useEffect, useMemo, useRef, useState } from "react";
import { CheckCircle2, Minus, Plus, ShieldAlert, X } from "lucide-react";
import { ApiError, api } from "../api/http";
import type { OptionsTopic, PaperPosition } from "../api/types";
import { useConfig } from "../api/useConfig";
import { fmt, fmtInt } from "../lib/format";
import { useTopic } from "../realtime/RealtimeProvider";
import { Badge } from "../ui/Badge";
import { Button } from "../ui/Button";
import { Dialog } from "../ui/Dialog";
import { cn } from "../ui/cn";
import { ErrorState, Skeleton } from "../ui/feedback";
import { Field, Input, Select } from "../ui/fields";
import { Segmented } from "../ui/Segmented";
import { useUI } from "../ui/UIProvider";

const MAX_QUANTITY = 100_000;
const QUICK_QUANTITIES = [25, 50, 75, 100, 200] as const;

export default function OrderDrawer() {
  const { orderOpen, closeOrder } = useUI();
  return (
    <Dialog open={orderOpen} onClose={closeOrder} label="New simulated order" placement="drawer">
      <OrderPanel />
    </Dialog>
  );
}

type Step = "form" | "review" | "done";

/** Validation for the quantity field. Returns an error message, or null when it is fine. */
export function quantityError(text: string): string | null {
  if (text.trim() === "") return "Enter a quantity.";
  const n = Number(text);
  if (!Number.isInteger(n)) return "Quantity must be a whole number.";
  if (n < 1) return "Quantity must be at least 1.";
  if (n > MAX_QUANTITY) return `Quantity cannot exceed ${fmtInt(MAX_QUANTITY)}.`;
  return null;
}

function OrderPanel() {
  const { orderPreset, closeOrder } = useUI();
  const { config } = useConfig();
  const chain = useTopic<OptionsTopic>(config ? `options:${config.underlying}` : null).data;

  const [side, setSide] = useState<"BUY" | "SELL">(orderPreset.side ?? "BUY");
  const [type, setType] = useState<"CE" | "PE">(orderPreset.optionType ?? "CE");
  const [strike, setStrike] = useState<string>(orderPreset.strike != null ? String(orderPreset.strike) : "atm");
  const [qty, setQty] = useState("50");
  const [step, setStep] = useState<Step>("form");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [filled, setFilled] = useState<PaperPosition | null>(null);
  const key = useRef(crypto.randomUUID());
  const headingRef = useRef<HTMLHeadingElement>(null);

  // One Idempotency-Key per distinct order: a retry of the same review reuses it, an edit starts a new one.
  useEffect(() => {
    key.current = crypto.randomUUID();
  }, [side, type, strike, qty]);

  // Move focus to the new step's heading so keyboard and screen-reader users land in the right place.
  useEffect(() => {
    headingRef.current?.focus();
  }, [step]);

  const row = useMemo(() => {
    if (!chain) return null;
    return strike === "atm" ? (chain.strikes.find((r) => r.is_atm) ?? null) : (chain.strikes.find((r) => r.strike === Number(strike)) ?? null);
  }, [chain, strike]);
  const ltp = row ? (type === "CE" ? row.call_ltp : row.put_ltp) : null;
  const quantity = Number(qty);
  const qError = quantityError(qty);
  const strikeError = chain && !row ? "That strike is no longer in the option chain." : null;
  const estimate = ltp !== null && !qError ? ltp * quantity : null;
  const valid = !qError && !strikeError && row !== null && ltp !== null && ltp > 0;
  const strikeLabel = row ? fmtInt(row.strike) : "ATM";

  function bump(delta: number) {
    const base = Number.isInteger(Number(qty)) && Number(qty) > 0 ? Number(qty) : 0;
    setQty(String(Math.min(MAX_QUANTITY, Math.max(1, base + delta))));
  }

  async function place() {
    if (!valid || busy) return;
    setBusy(true);
    setError(null);
    try {
      const body = {
        option_type: type,
        side,
        quantity,
        strike: strike === "atm" ? null : Number(strike),
        expiry: chain?.expiry ?? null,
        underlying: chain?.underlying ?? null,
      };
      const position = await api.post<PaperPosition>("/api/paper/open", body, { "Idempotency-Key": key.current });
      setFilled(position);
      setStep("done");
    } catch (err) {
      setError(err instanceof ApiError || err instanceof Error ? err.message : "The order could not be placed.");
    } finally {
      setBusy(false);
    }
  }

  function reset() {
    setStep("form");
    setFilled(null);
    setError(null);
  }

  return (
    <div className="flex max-h-[inherit] min-h-0 flex-col sm:h-dvh">
      <header className="flex items-start justify-between gap-3 border-b border-line px-5 py-4">
        <div className="min-w-0">
          <h2 ref={headingRef} tabIndex={-1} className="text-lg font-semibold tracking-tight outline-none">
            {step === "form" ? "New order" : step === "review" ? "Review order" : "Order filled"}
          </h2>
          <p className="mt-0.5 flex items-center gap-1.5 text-sm text-muted">
            <Badge tone="brand">Simulated</Badge>
            No real order is placed and no real money moves.
          </p>
        </div>
        <button type="button" onClick={closeOrder} aria-label="Close order panel" className="grid size-9 shrink-0 place-items-center rounded-control text-muted hover:bg-surface-2 hover:text-fg">
          <X aria-hidden="true" className="size-5" />
        </button>
      </header>

      <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
        {step === "form" && (
          <form
            id="order-form"
            className="grid gap-5"
            onSubmit={(e) => {
              e.preventDefault();
              if (valid) setStep("review");
            }}
            noValidate
          >
            <div className="grid grid-cols-2 gap-3">
              <div>
                <span id="side-label" className="mb-1.5 block text-sm font-medium">Side</span>
                <Segmented
                  label="Side"
                  value={side}
                  onChange={setSide}
                  options={[
                    { value: "BUY", label: "Buy" },
                    { value: "SELL", label: "Sell" },
                  ]}
                  className="w-full [&>button]:flex-1"
                />
              </div>
              <div>
                <span id="type-label" className="mb-1.5 block text-sm font-medium">Option</span>
                <Segmented
                  label="Option type"
                  value={type}
                  onChange={setType}
                  options={[
                    { value: "CE", label: "Call (CE)", title: "Call option" },
                    { value: "PE", label: "Put (PE)", title: "Put option" },
                  ]}
                  className="w-full [&>button]:flex-1"
                />
              </div>
            </div>

            <Field label="Strike" hint={chain ? `Expiry ${chain.expiry}` : "Waiting for the option chain…"} error={strikeError}>
              {(p) => (
                <Select {...p} value={strike} onChange={(e) => setStrike(e.target.value)} disabled={!chain}>
                  <option value="atm">At the money{chain?.strikes.find((r) => r.is_atm) ? ` (${fmtInt(chain.strikes.find((r) => r.is_atm)!.strike)})` : ""}</option>
                  {(chain?.strikes ?? []).map((r) => (
                    <option key={r.strike} value={String(r.strike)}>
                      {fmtInt(r.strike)}
                    </option>
                  ))}
                </Select>
              )}
            </Field>

            <Field label="Quantity" error={qError}>
              {(p) => (
                <>
                  <div className="flex items-stretch gap-2">
                    <Button aria-label="Decrease quantity" onClick={() => bump(-1)} disabled={Number(qty) <= 1}>
                      <Minus aria-hidden="true" className="size-4" />
                    </Button>
                    <Input {...p} inputMode="numeric" value={qty} onChange={(e) => setQty(e.target.value.replace(/[^\d]/g, ""))} className="text-center" />
                    <Button aria-label="Increase quantity" onClick={() => bump(1)} disabled={Number(qty) >= MAX_QUANTITY}>
                      <Plus aria-hidden="true" className="size-4" />
                    </Button>
                  </div>
                  <div className="flex flex-wrap gap-1.5" role="group" aria-label="Quick quantities">
                    {QUICK_QUANTITIES.map((q) => (
                      <button
                        key={q}
                        type="button"
                        onClick={() => setQty(String(q))}
                        aria-pressed={qty === String(q)}
                        className={cn("num min-h-8 rounded-full border px-3 text-xs font-medium", qty === String(q) ? "border-brand bg-brand-soft text-brand-fg" : "border-line text-muted hover:bg-surface-2")}
                      >
                        {q}
                      </button>
                    ))}
                  </div>
                </>
              )}
            </Field>

            <dl className="grid gap-2 rounded-card border border-line bg-surface-2 p-3 text-sm">
              <div className="flex items-center justify-between">
                <dt className="text-muted">Latest price</dt>
                <dd className="num font-medium">{chain ? (ltp === null ? "—" : fmt(ltp)) : <Skeleton className="h-4 w-16" />}</dd>
              </div>
              <div className="flex items-center justify-between">
                <dt className="text-muted">Estimated value</dt>
                <dd className="num text-base font-semibold">{estimate === null ? "—" : `₹ ${fmt(estimate)}`}</dd>
              </div>
            </dl>
            <p className="-mt-3 text-xs text-subtle">The simulated fill uses the price at the moment you confirm.</p>
          </form>
        )}

        {step === "review" && (
          <div className="grid gap-4">
            <dl className="grid gap-3 rounded-card border border-line p-4 text-sm">
              {[
                ["Contract", `${strikeLabel} ${type}`],
                ["Side", side === "BUY" ? "Buy" : "Sell"],
                ["Quantity", fmtInt(quantity)],
                ["Latest price", ltp === null ? "—" : fmt(ltp)],
                ["Estimated value", estimate === null ? "—" : `₹ ${fmt(estimate)}`],
                ["Expiry", chain?.expiry ?? "—"],
              ].map(([k, v]) => (
                <div key={k} className="flex items-center justify-between gap-4">
                  <dt className="text-muted">{k}</dt>
                  <dd className="num font-medium">{v}</dd>
                </div>
              ))}
            </dl>
            <p className="flex items-start gap-2 rounded-control bg-warn-soft p-3 text-sm text-warn">
              <ShieldAlert aria-hidden="true" className="mt-0.5 size-4 shrink-0" />
              This is a simulation. Confirming records a practice position in your account only.
            </p>
            {error && <ErrorState>{error}</ErrorState>}
          </div>
        )}

        {step === "done" && filled && (
          <div className="grid gap-4">
            <p role="status" className="flex items-center gap-2 text-pos">
              <CheckCircle2 aria-hidden="true" className="size-5" />
              <span className="font-medium">Simulated order placed</span>
            </p>
            <dl className="grid gap-3 rounded-card border border-line p-4 text-sm">
              {[
                ["Contract", `${fmtInt(filled.strike)} ${filled.option_type}`],
                ["Side", filled.side === "BUY" ? "Buy" : "Sell"],
                ["Quantity", fmtInt(filled.quantity)],
                ["Entry price", fmt(filled.entry_price)],
                ["Take profit at", fmt(filled.take_profit_price)],
                ["Stop loss at", fmt(filled.stop_loss_price)],
              ].map(([k, v]) => (
                <div key={k} className="flex items-center justify-between gap-4">
                  <dt className="text-muted">{k}</dt>
                  <dd className="num font-medium">{v}</dd>
                </div>
              ))}
            </dl>
          </div>
        )}
      </div>

      <footer className="flex gap-2 border-t border-line bg-surface px-5 py-4 pb-[max(1rem,env(safe-area-inset-bottom))]">
        {step === "form" && (
          <>
            <Button variant="secondary" onClick={closeOrder} className="flex-1">
              Cancel
            </Button>
            <Button type="submit" form="order-form" variant="primary" disabled={!valid} className="flex-[2]">
              Review order
            </Button>
          </>
        )}
        {step === "review" && (
          <>
            <Button variant="secondary" onClick={() => setStep("form")} disabled={busy} className="flex-1">
              Back
            </Button>
            <Button variant="primary" onClick={() => void place()} loading={busy} className="flex-[2]">
              Place simulated order
            </Button>
          </>
        )}
        {step === "done" && (
          <>
            <Button variant="secondary" onClick={reset} className="flex-1">
              Place another
            </Button>
            <Button variant="primary" onClick={closeOrder} className="flex-1">
              Done
            </Button>
          </>
        )}
      </footer>
    </div>
  );
}
