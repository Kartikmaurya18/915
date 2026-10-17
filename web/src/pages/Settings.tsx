import { useEffect, useState, type FormEvent } from "react";
import { ShieldAlert } from "lucide-react";
import { api } from "../api/http";
import type { SettingsPayload } from "../api/types";
import { Button } from "../ui/Button";
import { Card } from "../ui/Card";
import { ErrorState, FormMessage } from "../ui/feedback";
import { Field, Input, Select } from "../ui/fields";
import { PageHeader } from "../ui/layout";

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

  if (error && !settings) return <ErrorState>{error}</ErrorState>;
  if (!settings) return <p className="text-sm text-muted">Loading…</p>;
  return (
    <>
      <PageHeader title="Settings" description="Engine settings. Administrators only." />
      <p className="flex items-center gap-2 rounded-control bg-warn-soft px-3 py-2 text-sm text-warn">
        <ShieldAlert aria-hidden="true" className="size-4 shrink-0" />
        These settings are global: they change the market engine for every user.
      </p>
      <div className="grid gap-4 lg:grid-cols-3 lg:gap-5">
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
  return msg ? <FormMessage ok={msg.ok}>{msg.text}</FormMessage> : null;
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
    <Card title="ATM selection" description="How the at-the-money strike is chosen.">
      <form onSubmit={submit} className="grid gap-4">
        <Field label="Method">
          {(p) => (
            <Select {...p} value={method} onChange={(e) => setMethod(e.target.value)}>
              <option value="delta">Delta-based</option>
              <option value="price">Nearest to spot (price)</option>
            </Select>
          )}
        </Field>
        <Field label="Delta threshold">{(p) => <Input {...p} inputMode="decimal" value={threshold} onChange={(e) => setThreshold(e.target.value)} />}</Field>
        <div className="flex flex-wrap items-center gap-3">
          <Button type="submit" loading={busy}>Save</Button>
          <Feedback msg={msg} />
        </div>
      </form>
    </Card>
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
    <Card title="Candle price basis" description="A display choice only; it does not change the CE+PE formula.">
      <form onSubmit={submit} className="grid gap-4">
        <Field label="Series used for candlesticks">
          {(p) => (
            <Select {...p} value={basis} onChange={(e) => setBasis(e.target.value)}>
              <option value="underlying">Underlying spot</option>
              <option value="combined_value">ATM combined value (CE+PE)</option>
            </Select>
          )}
        </Field>
        <div className="flex flex-wrap items-center gap-3">
          <Button type="submit" loading={busy}>Save</Button>
          <Feedback msg={msg} />
        </div>
      </form>
    </Card>
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
    void run(() => api.post("/api/settings/data-source", { data_source: source, sequence: replay ? sequence : null, speed: replay ? Number(speed) : null }));
  };
  return (
    <Card title="Data source" description="Switch between the live simulation and a replay of a recorded sequence.">
      <form onSubmit={submit} className="grid gap-4">
        <Field label="Source">
          {(p) => (
            <Select {...p} value={source} onChange={(e) => setSource(e.target.value)}>
              {["mock", "replay"].map((s) => (
                <option key={s} value={s}>
                  {s === "replay" ? "Replay / test mode" : "Mock (live simulation)"}
                </option>
              ))}
            </Select>
          )}
        </Field>
        {replay && (
          <>
            <Field label="Replay sequence">
              {(p) => (
                <Select {...p} value={sequence} onChange={(e) => setSequence(e.target.value)}>
                  {sequences.map((s) => (
                    <option key={s} value={s}>
                      {s.replace(/_/g, " ")}
                    </option>
                  ))}
                </Select>
              )}
            </Field>
            <Field label="Speed">{(p) => <Input {...p} inputMode="decimal" value={speed} onChange={(e) => setSpeed(e.target.value)} />}</Field>
          </>
        )}
        <div className="flex flex-wrap items-center gap-3">
          <Button type="submit" loading={busy}>Apply</Button>
          <Feedback msg={msg} />
        </div>
      </form>
    </Card>
  );
}
