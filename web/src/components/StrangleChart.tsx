import { useMemo, useState } from "react";
import { Crosshair } from "lucide-react";
import type { Bar, StrikeRow } from "../api/types";
import { usePairCandles } from "../api/usePairCandles";
import { fmt, fmtInt, fmtTime } from "../lib/format";
import { Badge, DeltaBadge } from "../ui/Badge";
import { Button } from "../ui/Button";
import { Card } from "../ui/Card";
import { EmptyState, ErrorState } from "../ui/feedback";
import { Select } from "../ui/fields";
import { Segmented } from "../ui/Segmented";
import { CandleChart, describeSeries } from "./CandleChart";

const INTERVALS = ["1", "5", "15"] as const;

interface Props {
  rows: readonly StrikeRow[];
  atm: number | null;
  expiry: string | null;
}

/** Strikes `steps` away from the money on each side, snapped to what the chain lists. */
export function symmetricPair(rows: readonly StrikeRow[], atm: number | null, steps: number): { ce: number; pe: number } | null {
  const sorted = [...rows].sort((a, b) => a.strike - b.strike);
  const at = sorted.findIndex((r) => r.strike === atm);
  if (at < 0) return null;
  const ce = sorted[at + steps]?.strike;
  const pe = sorted[at - steps]?.strike;
  return ce === undefined || pe === undefined ? null : { ce, pe };
}

export function summariseBars(bars: readonly Bar[]): { open: number; high: number; low: number; close: number; changePct: number } | null {
  if (bars.length === 0) return null;
  const open = bars[0]!.open;
  const close = bars[bars.length - 1]!.close;
  return { open, close, high: Math.max(...bars.map((b) => b.high)), low: Math.min(...bars.map((b) => b.low)), changePct: open ? ((close - open) / open) * 100 : 0 };
}

/**
 * Live combined premium of a call and a put at chosen strikes: a straddle when they match, a strangle when they
 * do not. Strikes are fixed to the contracts picked, so the series never silently re-points when ATM moves.
 */
export function StrangleChart({ rows, atm, expiry }: Props) {
  const strikes = useMemo(() => rows.map((r) => r.strike).sort((a, b) => a - b), [rows]);
  const [ce, setCe] = useState<number | null>(null);
  const [pe, setPe] = useState<number | null>(null);
  const [interval, setIntervalKey] = useState<(typeof INTERVALS)[number]>("1");

  // Until the user chooses, start from a +-2 strangle around the money as it stood when the chain first arrived.
  const [seeded, setSeeded] = useState(false);
  if (!seeded && rows.length > 0) {
    const pair = symmetricPair(rows, atm, 2);
    if (pair) {
      setCe(pair.ce);
      setPe(pair.pe);
      setSeeded(true);
    }
  }

  const { data, unavailable, error } = usePairCandles(ce, pe);
  const bars = useMemo(() => data?.by_interval?.[interval]?.pair ?? [], [data, interval]);
  const stats = useMemo(() => summariseBars(bars), [bars]);
  const live = useMemo(() => {
    const c = rows.find((r) => r.strike === ce)?.call_ltp;
    const p = rows.find((r) => r.strike === pe)?.put_ltp;
    return c != null && p != null ? c + p : null;
  }, [rows, ce, pe]);
  const kind = ce !== null && ce === pe ? "Straddle" : "Strangle";
  const pick = (steps: number) => {
    const pair = symmetricPair(rows, atm, steps);
    if (pair) {
      setCe(pair.ce);
      setPe(pair.pe);
    }
  };

  return (
    <Card
      aria-label="Strangle chart"
      title={
        <span className="flex flex-wrap items-center gap-2">
          {kind} · {ce === null ? "—" : `${fmtInt(ce)} CE`} + {pe === null ? "—" : `${fmtInt(pe)} PE`}
          {expiry && <span className="text-sm font-normal text-muted">{expiry}</span>}
        </span>
      }
      description="Combined premium of the two contracts you pick. Candles are exact for the sum, and start when tracking started."
      actions={<Segmented label="Strangle interval" size="sm" value={interval} onChange={setIntervalKey} options={INTERVALS.map((i) => ({ value: i, label: `${i}m`, title: `${i} minute candles` }))} />}
    >
      <div className="mb-3 flex flex-wrap items-end gap-3">
        <label className="grid gap-1 text-sm">
          <span className="text-muted">Call strike</span>
          <Select value={ce ?? ""} onChange={(e) => setCe(Number(e.target.value))} className="min-h-8 w-32 py-1">
            {strikes.map((k) => <option key={k} value={k}>{fmtInt(k)}</option>)}
          </Select>
        </label>
        <label className="grid gap-1 text-sm">
          <span className="text-muted">Put strike</span>
          <Select value={pe ?? ""} onChange={(e) => setPe(Number(e.target.value))} className="min-h-8 w-32 py-1">
            {strikes.map((k) => <option key={k} value={k}>{fmtInt(k)}</option>)}
          </Select>
        </label>
        <div role="group" aria-label="Quick pairs" className="flex flex-wrap gap-1.5">
          <Button size="sm" onClick={() => pick(0)}>ATM straddle</Button>
          {[1, 2, 3].map((n) => <Button key={n} size="sm" onClick={() => pick(n)}>±{n} strangle</Button>)}
        </div>
      </div>

      <div className="mb-2 flex flex-wrap items-baseline gap-x-4 gap-y-1">
        <span className="num text-2xl font-semibold tracking-tight">{fmt(live ?? stats?.close)}</span>
        {stats && <DeltaBadge value={stats.changePct} percent />}
        {stats && <span className="num text-sm text-muted">Open {fmt(stats.open)} · High {fmt(stats.high)} · Low {fmt(stats.low)}</span>}
      </div>

      {ce === null || pe === null || (unavailable && bars.length === 0) ? (
        <EmptyState title={ce === null ? "Waiting for the option chain" : "These strikes are not in the live chain"} icon={<Crosshair className="size-5" />}>
          {ce === null ? "Pick strikes once the chain has loaded." : "Choose strikes that are listed in the option chain."}
        </EmptyState>
      ) : bars.length === 0 ? (
        <EmptyState title="Tracking started" icon={<Crosshair className="size-5" />}>The first candle appears with the next tick.</EmptyState>
      ) : (
        <CandleChart series={bars} resetKey={`${ce}:${pe}:${interval}`} summary={describeSeries(`${kind} of ${fmtInt(ce)} call and ${fmtInt(pe)} put, ${interval} minute candles`, bars, "candles")} height="clamp(220px, 36vw, 340px)" />
      )}
      {data && (
        <p className="mt-2 text-xs text-muted">
          Tracking since {fmtTime(data.tracked_since)}. Earlier prices of this pair were not recorded, so the chart does not show them. <Badge tone="neutral">{data.expiry}</Badge>
        </p>
      )}
      {error && <ErrorState className="mt-3">{error}</ErrorState>}
    </Card>
  );
}
