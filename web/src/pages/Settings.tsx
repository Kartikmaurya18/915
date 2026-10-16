import { useEffect, useState, type FormEvent } from "react";
import { api } from "../api/http";
import type { SettingsPayload } from "../api/types";

type Msg = { ok: boolean; text: string } | null;

export default function Settings() {
  const [settings, setSettings] = useState<SettingsPayload | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = () =>
    api
      .get<SettingsPayload>("/api/settings")
      .then((s) => {
        setSettings(s);
        setError(null);
      })
      .catch((err: unknown) => setError(err instanceof Error ? err.message : "Could not load settings"));
  useEffect(() => {
    void load();
  }, []);

  if (error && !settings) return <p className="form-error" role="alert">{error}</p>;
  if (!settings) return <p className="muted">Loading…</p>;
  return (
    <>
      <p className="notice-line">These settings are global: they change the market engine for every user.</p>
      <div className="grid-2">
        <AtmForm settings={settings} onSaved={load} />
        <CandleBasisForm settings={settings} onSaved={load} />
        <DataSourceForm settings={settings} onSaved={load} />
      </div>
    </>
  );
}

function useSave(onSaved: () => Promise<unknown>) {
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<Msg>(null);
  async function run(call: () => Promise<unknown>) {
    setBusy(true);
    setMsg(null);
    try {
      await call();
      await onSaved();
      setMsg({ ok: true, text: "Saved" });
    } catch (err) {
      setMsg({ ok: false, text: err instanceof Error ? err.message : "Could not save" });
    } finally {
      setBusy(false);
    }
  }
  return { busy, msg, run };
}

function Feedback({ msg }: { msg: Msg }) {
  if (!msg) return null;
  return <p className={msg.ok ? "form-ok" : "form-error"} role={msg.ok ? "status" : "alert"}>{msg.text}</p>;
}

function AtmForm({ settings, onSaved }: { settings: SettingsPayload; onSaved: () => Promise<unknown> }) {
  const [method, setMethod] = useState(settings.atm_method);
  const [threshold, setThreshold] = useState(String(settings.delta_threshold));
  const { busy, msg, run } = useSave(onSaved);
  const submit = (e: FormEvent) => {
    e.preventDefault();
    void run(() => api.post("/api/settings/atm", { atm_method: method, delta_threshold: Number(threshold) }));
  };
  return (
    <form className="panel form" onSubmit={submit}>
      <h2>ATM selection</h2>
      <label>
        Method
        <select value={method} onChange={(e) => setMethod(e.target.value)}>
          <option value="delta">Delta-based</option>
          <option value="price">Nearest to spot (price)</option>
        </select>
      </label>
      <label>
        Delta threshold
        <input inputMode="decimal" value={threshold} onChange={(e) => setThreshold(e.target.value)} />
      </label>
      <button className="btn" disabled={busy}>Save</button>
      <Feedback msg={msg} />
    </form>
  );
}

function CandleBasisForm({ settings, onSaved }: { settings: SettingsPayload; onSaved: () => Promise<unknown> }) {
  const [basis, setBasis] = useState(settings.candle_price_basis);
  const { busy, msg, run } = useSave(onSaved);
  const submit = (e: FormEvent) => {
    e.preventDefault();
    void run(() => api.post("/api/settings/candle-basis", { candle_price_basis: basis }));
  };
  return (
    <form className="panel form" onSubmit={submit}>
      <h2>Candle price basis</h2>
      <label>
        Series used for candlesticks
        <select value={basis} onChange={(e) => setBasis(e.target.value)}>
          <option value="underlying">Underlying spot</option>
          <option value="combined_value">ATM combined value (CE+PE)</option>
        </select>
      </label>
      <p className="hint">Display choice only; it does not change the CE+PE formula.</p>
      <button className="btn" disabled={busy}>Save</button>
      <Feedback msg={msg} />
    </form>
  );
}

function DataSourceForm({ settings, onSaved }: { settings: SettingsPayload; onSaved: () => Promise<unknown> }) {
  const [source, setSource] = useState(settings.data_source);
  const sequences = settings.replay?.available_sequences ?? [];
  const [sequence, setSequence] = useState(settings.replay?.sequence ?? sequences[0] ?? "");
  const [speed, setSpeed] = useState(String(settings.replay?.speed ?? 1));
  const { busy, msg, run } = useSave(onSaved);
  const replay = source === "replay";
  const submit = (e: FormEvent) => {
    e.preventDefault();
    void run(() =>
      api.post("/api/settings/data-source", {
        data_source: source,
        sequence: replay ? sequence : null,
        speed: replay ? Number(speed) : null,
      }),
    );
  };
  return (
    <form className="panel form" onSubmit={submit}>
      <h2>Data source</h2>
      <label>
        Source
        <select value={source} onChange={(e) => setSource(e.target.value)}>
          {["mock", "replay"].map((s) => (
            <option key={s} value={s}>{s === "replay" ? "Replay / test mode" : "Mock (live simulation)"}</option>
          ))}
        </select>
      </label>
      {replay && (
        <>
          <label>
            Replay sequence
            <select value={sequence} onChange={(e) => setSequence(e.target.value)}>
              {sequences.map((s) => (
                <option key={s} value={s}>{s.replace(/_/g, " ")}</option>
              ))}
            </select>
          </label>
          <label>
            Speed
            <input inputMode="decimal" value={speed} onChange={(e) => setSpeed(e.target.value)} />
          </label>
        </>
      )}
      <button className="btn" disabled={busy}>Apply</button>
      <Feedback msg={msg} />
    </form>
  );
}
