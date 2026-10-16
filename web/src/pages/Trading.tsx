import { useEffect, useRef, useState, type FormEvent } from "react";
import { useSearchParams } from "react-router-dom";
import { api } from "../api/http";
import type { OpenPositionBody, PaperConfig, PaperPosition, PaperState } from "../api/types";
import { usePaperState } from "../api/usePaperState";
import { fmt, fmtDateTime, fmtInt, fmtSigned, pnlClass } from "../lib/format";

export default function Trading() {
  const { state, error, refresh } = usePaperState();
  return (
    <>
      <p className="notice-line">Simulated trading only. Prices come from the live feed; no real orders are placed.</p>
      {error && !state && <p className="form-error" role="alert">{error}</p>}
      <Summary state={state} />
      <div className="grid-2">
        <OrderForm onDone={refresh} />
        <ConfigForm config={state?.config} onDone={refresh} />
      </div>
      <Positions title="Open positions" rows={state?.open_positions ?? []} onClose={refresh} open />
      <Positions title="History" rows={state?.history ?? []} onClose={refresh} />
    </>
  );
}

function Summary({ state }: { state: PaperState | null }) {
  const p = state?.pnl_summary;
  const s = state?.stats;
  return (
    <section className="metrics" aria-label="Paper trading summary">
      <div className="metric">
        <span className="metric-label">Total P&amp;L</span>
        <strong className={`metric-value ${pnlClass(p?.total_pnl)}`}>{fmtSigned(p?.total_pnl)}</strong>
        <span className="metric-sub">Realised {fmtSigned(p?.realized_pnl)} · Unrealised {fmtSigned(p?.unrealized_pnl)}</span>
      </div>
      <div className="metric">
        <span className="metric-label">Open positions</span>
        <strong className="metric-value">{p ? p.open_position_count : "—"}</strong>
      </div>
      <div className="metric">
        <span className="metric-label">Win rate</span>
        <strong className="metric-value">{s ? `${fmt(s.win_rate_percent, 1)}%` : "—"}</strong>
        <span className="metric-sub">{s ? `${s.wins}W · ${s.losses}L of ${s.total_trades}` : undefined}</span>
      </div>
      <div className="metric">
        <span className="metric-label">Max drawdown</span>
        <strong className="metric-value">{s ? fmt(s.max_drawdown) : "—"}</strong>
      </div>
    </section>
  );
}

function newKey(): string {
  return crypto.randomUUID();
}

function OrderForm({ onDone }: { onDone: () => Promise<void> }) {
  const [params] = useSearchParams();
  const [type, setType] = useState<"CE" | "PE">(params.get("type") === "PE" ? "PE" : "CE");
  const [side, setSide] = useState<"BUY" | "SELL">("BUY");
  const [strike, setStrike] = useState(params.get("strike") ?? "");
  const [qty, setQty] = useState("50");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null);

  // One Idempotency-Key per distinct order. A retry of the same order (e.g. after a dropped response)
  // reuses it and cannot open a second position; changing any field starts a new order.
  const key = useRef(newKey());
  useEffect(() => {
    key.current = newKey();
  }, [type, side, strike, qty]);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (busy) return;
    const quantity = Number(qty);
    if (!Number.isInteger(quantity) || quantity <= 0) {
      setMessage({ ok: false, text: "Quantity must be a whole number above zero." });
      return;
    }
    const body: OpenPositionBody = { option_type: type, side, quantity, strike: strike === "" ? null : Number(strike) };
    setBusy(true);
    setMessage(null);
    try {
      const pos = await api.post<PaperPosition>("/api/paper/open", body, { "Idempotency-Key": key.current });
      setMessage({ ok: true, text: `${pos.side} ${pos.quantity} × ${fmtInt(pos.strike)} ${pos.option_type} at ${fmt(pos.entry_price)}` });
      key.current = newKey();
      await onDone();
    } catch (err) {
      setMessage({ ok: false, text: err instanceof Error ? err.message : "Order failed" });
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="panel form" onSubmit={submit}>
      <h2>New simulated position</h2>
      <div className="row">
        <fieldset className="seg" aria-label="Option type">
          {(["CE", "PE"] as const).map((t) => (
            <button key={t} type="button" className={type === t ? "seg-btn on" : "seg-btn"} aria-pressed={type === t} onClick={() => setType(t)}>
              {t}
            </button>
          ))}
        </fieldset>
        <fieldset className="seg" aria-label="Side">
          {(["BUY", "SELL"] as const).map((s) => (
            <button key={s} type="button" className={side === s ? "seg-btn on" : "seg-btn"} aria-pressed={side === s} onClick={() => setSide(s)}>
              {s}
            </button>
          ))}
        </fieldset>
      </div>
      <label>
        Strike <span className="hint">(blank = at the money)</span>
        <input inputMode="decimal" value={strike} onChange={(e) => setStrike(e.target.value)} placeholder="ATM" />
      </label>
      <label>
        Quantity
        <input inputMode="numeric" value={qty} onChange={(e) => setQty(e.target.value)} />
      </label>
      <button className="btn btn-primary" type="submit" disabled={busy}>
        {busy ? "Placing…" : "Place simulated order"}
      </button>
      {message && (
        <p className={message.ok ? "form-ok" : "form-error"} role={message.ok ? "status" : "alert"}>
          {message.text}
        </p>
      )}
    </form>
  );
}

function ConfigForm({ config, onDone }: { config: PaperConfig | undefined; onDone: () => Promise<void> }) {
  const [tp, setTp] = useState("");
  const [sl, setSl] = useState("");
  const [tpAmt, setTpAmt] = useState("");
  const [slAmt, setSlAmt] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null);
  const loaded = useRef(false);

  // Fill the form once from the saved values; never overwrite what the user is typing.
  useEffect(() => {
    if (!config || loaded.current) return;
    loaded.current = true;
    setTp(String(config.take_profit_percent));
    setSl(String(config.stop_loss_percent));
    setTpAmt(config.take_profit_amount === null ? "" : String(config.take_profit_amount));
    setSlAmt(config.stop_loss_amount === null ? "" : String(config.stop_loss_amount));
  }, [config]);

  const num = (v: string) => (v.trim() === "" ? null : Number(v));

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setMessage(null);
    try {
      await api.post("/api/paper/config", {
        take_profit_percent: num(tp),
        stop_loss_percent: num(sl),
        take_profit_amount: num(tpAmt),
        stop_loss_amount: num(slAmt),
      });
      setMessage({ ok: true, text: "Saved. Applies to positions opened from now on." });
      await onDone();
    } catch (err) {
      setMessage({ ok: false, text: err instanceof Error ? err.message : "Could not save" });
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="panel form" onSubmit={submit}>
      <h2>Take profit / stop loss</h2>
      <div className="row">
        <label>
          Take profit %
          <input inputMode="decimal" value={tp} onChange={(e) => setTp(e.target.value)} />
        </label>
        <label>
          Stop loss %
          <input inputMode="decimal" value={sl} onChange={(e) => setSl(e.target.value)} />
        </label>
      </div>
      <div className="row">
        <label>
          Profit target ₹ <span className="hint">(optional)</span>
          <input inputMode="decimal" value={tpAmt} onChange={(e) => setTpAmt(e.target.value)} />
        </label>
        <label>
          Loss limit ₹ <span className="hint">(optional)</span>
          <input inputMode="decimal" value={slAmt} onChange={(e) => setSlAmt(e.target.value)} />
        </label>
      </div>
      <button className="btn" type="submit" disabled={busy}>
        {busy ? "Saving…" : "Save"}
      </button>
      {message && (
        <p className={message.ok ? "form-ok" : "form-error"} role={message.ok ? "status" : "alert"}>
          {message.text}
        </p>
      )}
    </form>
  );
}

function Positions({ title, rows, onClose, open = false }: { title: string; rows: PaperPosition[]; onClose: () => Promise<void>; open?: boolean }) {
  const [closing, setClosing] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function close(id: string) {
    setClosing(id);
    setError(null);
    try {
      await api.post(`/api/paper/close/${encodeURIComponent(id)}`);
    } catch (err) {
      // 404 means it was already closed (for example by a take-profit just now): not an error to show.
      const status = (err as { status?: number }).status;
      if (status !== 404) setError(err instanceof Error ? err.message : "Could not close the position");
    } finally {
      setClosing(null);
      await onClose();
    }
  }

  return (
    <section className="panel">
      <header className="panel-head">
        <h2>{title}</h2>
        <span className="muted">{rows.length}</span>
      </header>
      {error && <p className="form-error" role="alert">{error}</p>}
      <div className="table-wrap">
        <table className="data">
          <thead>
            <tr>
              <th>Contract</th>
              <th className="r">Qty</th>
              <th className="r">Entry</th>
              <th className="r">{open ? "Now" : "Exit"}</th>
              <th className="r">P&amp;L</th>
              {open ? <th className="r">TP / SL</th> : <th>Reason</th>}
              <th>{open ? "" : "Closed"}</th>
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 && (
              <tr>
                <td colSpan={7} className="empty">{open ? "No open positions" : "No closed trades yet"}</td>
              </tr>
            )}
            {rows.map((p) => (
              <tr key={p.id}>
                <td>
                  <span className={p.option_type === "CE" ? "call" : "put"}>{fmtInt(p.strike)} {p.option_type}</span>{" "}
                  <span className="tag">{p.side}</span>
                </td>
                <td className="r">{fmtInt(p.quantity)}</td>
                <td className="r">{fmt(p.entry_price)}</td>
                <td className="r">{fmt(open ? p.current_price : p.exit_price)}</td>
                <td className={`r ${pnlClass(p.pnl)}`}>
                  {fmtSigned(p.pnl)} <span className="muted">({fmtSigned(p.pnl_percent)}%)</span>
                </td>
                {open ? (
                  <td className="r">{fmt(p.take_profit_price)} / {fmt(p.stop_loss_price)}</td>
                ) : (
                  <td>{p.exit_reason ?? "—"}</td>
                )}
                <td>
                  {open ? (
                    <button className="btn btn-small" disabled={closing === p.id} onClick={() => void close(p.id)}>
                      {closing === p.id ? "Closing…" : "Close"}
                    </button>
                  ) : (
                    fmtDateTime(p.exit_time)
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
