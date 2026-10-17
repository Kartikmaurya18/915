import { memo, useEffect, useId, useMemo, useRef, useState, type PointerEvent } from "react";
import { fmt, fmtInt, fmtSigned } from "../lib/format";

interface PayoffChartProps {
  spots: readonly number[];
  /** Profit or loss at expiry for each level in `spots`. */
  expiry: readonly number[];
  /** Model value today at current IV; omitted when IV or time to expiry is unknown. */
  today?: readonly number[] | null;
  spot: number | null;
  breakevens: readonly number[];
}

const M = { top: 14, right: 16, bottom: 28, left: 56 };
/** Behind every label so it stays legible where it crosses the curve or the fills. */
const HALO = { paintOrder: "stroke", stroke: "var(--surface)", strokeWidth: 4, strokeLinejoin: "round" } as const;

/**
 * Expiry payoff of a strategy as accessible SVG. Hand-drawn rather than a chart library: it is a static
 * piecewise-linear curve, and SVG lets the profit and loss regions be filled exactly, with every mark
 * available to assistive technology through the summary label and the data table below it.
 */
export const PayoffChart = memo(function PayoffChart({ spots, expiry, today, spot, breakevens }: PayoffChartProps) {
  const clip = useId();
  const [hover, setHover] = useState<number | null>(null);
  // Drawn at the real pixel width so text stays 11px on a phone instead of scaling down with the viewBox.
  const box = useRef<HTMLDivElement>(null);
  const [W, setW] = useState(640);
  useEffect(() => {
    const el = box.current;
    if (!el) return;
    const measure = () => setW(Math.max(280, Math.round(el.clientWidth)));
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  const H = W < 480 ? 240 : 300;

  const geo = useMemo(() => {
    const all = [...expiry, ...(today ?? []), 0];
    let lo = Math.min(...all), hi = Math.max(...all);
    if (hi === lo) { hi += 1; lo -= 1; }
    const pad = (hi - lo) * 0.08;
    lo -= pad; hi += pad;
    const x0 = spots[0]!, x1 = spots[spots.length - 1]!;
    const sx = (x: number) => M.left + ((x - x0) / (x1 - x0)) * (W - M.left - M.right);
    const sy = (y: number) => M.top + ((hi - y) / (hi - lo)) * (H - M.top - M.bottom);
    return { lo, hi, x0, x1, sx, sy };
  }, [spots, expiry, today, W, H]);

  const { sx, sy } = geo;
  const line = (ys: readonly number[]) => ys.map((y, i) => `${i === 0 ? "M" : "L"}${sx(spots[i]!).toFixed(1)},${sy(y).toFixed(1)}`).join("");
  const zeroY = sy(0);
  const area = `${line(expiry)}L${sx(spots[spots.length - 1]!).toFixed(1)},${zeroY.toFixed(1)}L${sx(spots[0]!).toFixed(1)},${zeroY.toFixed(1)}Z`;

  const yTicks = useMemo(() => {
    const step = niceStep((geo.hi - geo.lo) / 4);
    const first = Math.ceil(geo.lo / step) * step;
    const out: number[] = [];
    for (let v = first; v <= geo.hi; v += step) out.push(Math.abs(v) < step / 1e6 ? 0 : v);
    return out;
  }, [geo.hi, geo.lo]);
  const xTicks = useMemo(() => {
    const step = niceStep((geo.x1 - geo.x0) / 5);
    const out: number[] = [];
    for (let v = Math.ceil(geo.x0 / step) * step; v <= geo.x1; v += step) out.push(v);
    return out;
  }, [geo.x0, geo.x1]);

  function onMove(e: PointerEvent<SVGSVGElement>) {
    const rect = e.currentTarget.getBoundingClientRect();
    const px = ((e.clientX - rect.left) / rect.width) * W;
    const level = geo.x0 + ((px - M.left) / (W - M.left - M.right)) * (geo.x1 - geo.x0);
    let best = 0;
    for (let i = 1; i < spots.length; i++) if (Math.abs(spots[i]! - level) < Math.abs(spots[best]! - level)) best = i;
    setHover(px < M.left || px > W - M.right ? null : best);
  }

  const maxP = Math.max(...expiry), minP = Math.min(...expiry);
  const label = `Profit and loss at expiry across underlying levels ${fmtInt(geo.x0)} to ${fmtInt(geo.x1)}: best ${fmtSigned(maxP)}, worst ${fmtSigned(minP)}${breakevens.length ? `, breaks even at ${breakevens.map((b) => fmtInt(b)).join(" and ")}` : ""}.`;
  const rows = useMemo(() => {
    const n = 11;
    return Array.from({ length: n }, (_, k) => Math.round((k * (spots.length - 1)) / (n - 1)));
  }, [spots.length]);

  return (
    <div ref={box}>
      <svg width={W} height={H} viewBox={`0 0 ${W} ${H}`} role="img" aria-label={label} className="block max-w-full touch-pan-y select-none" onPointerMove={onMove} onPointerLeave={() => setHover(null)}>
        <defs>
          <clipPath id={`${clip}-up`}><rect x={0} y={0} width={W} height={zeroY} /></clipPath>
          <clipPath id={`${clip}-down`}><rect x={0} y={zeroY} width={W} height={H} /></clipPath>
        </defs>
        {yTicks.map((v) => (
          <g key={`y${v}`}>
            <line x1={M.left} x2={W - M.right} y1={sy(v)} y2={sy(v)} style={{ stroke: "var(--line)" }} strokeWidth={v === 0 ? 0 : 1} />
            <text x={M.left - 8} y={sy(v)} textAnchor="end" dominantBaseline="middle" className="num" style={{ fill: "var(--muted)", fontSize: 11 }}>{fmtCompactSigned(v)}</text>
          </g>
        ))}
        {xTicks.map((v) => (
          <text key={`x${v}`} x={sx(v)} y={H - 8} textAnchor="middle" className="num" style={{ fill: "var(--muted)", fontSize: 11 }}>{fmtInt(v)}</text>
        ))}
        <path d={area} clipPath={`url(#${clip}-up)`} style={{ fill: "var(--pos)", opacity: 0.16 }} />
        <path d={area} clipPath={`url(#${clip}-down)`} style={{ fill: "var(--neg)", opacity: 0.16 }} />
        <line x1={M.left} x2={W - M.right} y1={zeroY} y2={zeroY} style={{ stroke: "var(--fg)", opacity: 0.55 }} strokeWidth={1} />
        {spot !== null && spot >= geo.x0 && spot <= geo.x1 && (
          <g>
            <line x1={sx(spot)} x2={sx(spot)} y1={M.top} y2={H - M.bottom} style={{ stroke: "var(--brand)" }} strokeWidth={1.5} strokeDasharray="4 3" />
            <text x={sx(spot) + 4} y={M.top + 10} style={{ fill: "var(--brand-fg)", fontSize: 11, fontWeight: 600, ...HALO }}>Spot {fmtInt(spot)}</text>
          </g>
        )}
        {today && <path d={line(today)} fill="none" style={{ stroke: "var(--fg)" }} strokeWidth={1.75} strokeDasharray="6 4" />}
        <path d={line(expiry)} fill="none" style={{ stroke: "var(--fg)" }} strokeWidth={2.25} strokeLinejoin="round" />
        {breakevens.map((b) => b >= geo.x0 && b <= geo.x1 && (
          <g key={`be${b}`}>
            <circle cx={sx(b)} cy={zeroY} r={4.5} style={{ fill: "var(--surface)", stroke: "var(--fg)" }} strokeWidth={2} />
            <text x={sx(b)} y={zeroY - 9} textAnchor="middle" className="num" style={{ fill: "var(--fg)", fontSize: 11, fontWeight: 600, ...HALO }}>{fmt(b, 0)}</text>
          </g>
        ))}
        {hover !== null && (
          <g pointerEvents="none">
            <line x1={sx(spots[hover]!)} x2={sx(spots[hover]!)} y1={M.top} y2={H - M.bottom} style={{ stroke: "var(--muted)" }} strokeWidth={1} />
            <circle cx={sx(spots[hover]!)} cy={sy(expiry[hover]!)} r={4} style={{ fill: "var(--fg)" }} />
          </g>
        )}
      </svg>

      <div className="mt-2 flex flex-wrap items-center gap-x-5 gap-y-1 text-xs text-muted" aria-hidden="true">
        <span className="inline-flex items-center gap-1.5"><svg width="22" height="6"><line x1="0" x2="22" y1="3" y2="3" style={{ stroke: "var(--fg)" }} strokeWidth="2.25" /></svg>At expiry (exact)</span>
        {today && <span className="inline-flex items-center gap-1.5"><svg width="22" height="6"><line x1="0" x2="22" y1="3" y2="3" style={{ stroke: "var(--fg)" }} strokeWidth="1.75" strokeDasharray="5 3" /></svg>Today (model estimate)</span>}
        <span className="num ml-auto min-h-5 text-fg" aria-live="off">
          {hover !== null ? `At ${fmtInt(spots[hover]!)}: ${fmtSigned(expiry[hover]!)} at expiry${today ? `, ${fmtSigned(today[hover]!)} today` : ""}` : "Move over the chart to read a level"}
        </span>
      </div>

      <details className="mt-3 text-sm">
        <summary className="inline-flex min-h-8 cursor-pointer items-center text-brand-fg">View as a table</summary>
        <table className="mt-2 w-full text-sm">
          <caption className="sr-only">Profit and loss by underlying level</caption>
          <thead>
            <tr className="text-left text-xs text-subtle"><th className="py-1 pr-3 font-medium">Underlying</th><th className="py-1 pr-3 text-right font-medium">At expiry</th>{today && <th className="py-1 text-right font-medium">Today (model)</th>}</tr>
          </thead>
          <tbody>
            {rows.map((i) => (
              <tr key={i} className="num border-t border-line/70"><td className="py-1 pr-3">{fmtInt(spots[i]!)}</td><td className="py-1 pr-3 text-right">{fmtSigned(expiry[i]!)}</td>{today && <td className="py-1 text-right">{fmtSigned(today[i]!)}</td>}</tr>
            ))}
          </tbody>
        </table>
      </details>
    </div>
  );
});

/** A tick spacing of 1, 2, 2.5, 5 or 10 times a power of ten. */
export function niceStep(raw: number): number {
  if (!(raw > 0)) return 1;
  const pow = 10 ** Math.floor(Math.log10(raw));
  const f = raw / pow;
  return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10) * pow;
}

function fmtCompactSigned(v: number): string {
  const a = Math.abs(v);
  const body = a >= 1e7 ? `${(a / 1e7).toFixed(1)}Cr` : a >= 1e5 ? `${(a / 1e5).toFixed(1)}L` : a >= 1e3 ? `${(a / 1e3).toFixed(a % 1e3 === 0 ? 0 : 1)}k` : `${a}`;
  return v < 0 ? `−${body}` : body;
}
