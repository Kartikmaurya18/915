import { useMemo } from "react";
import { Orbit } from "lucide-react";
import { useConfig } from "../api/useConfig";
import type { RrgPoint, RrgTopic } from "../api/types";
import { fmt, fmtInt } from "../lib/format";
import { useTopic } from "../realtime/RealtimeProvider";
import { Badge } from "../ui/Badge";
import { Card } from "../ui/Card";
import { cn } from "../ui/cn";
import { EmptyState } from "../ui/feedback";
import { PageHeader, R, TABLE, TD, TH, TableWrap } from "../ui/layout";

// Each quadrant has a name, a tone for the label (text is always shown, never colour alone) and a fill for the plot.
const QUADRANT: Record<string, { tone: "pos" | "brand" | "warn" | "neg"; fill: string; label: string }> = {
  Leading: { tone: "pos", fill: "var(--pos)", label: "Leading: strong and gaining" },
  Improving: { tone: "brand", fill: "var(--brand)", label: "Improving: weak but gaining" },
  Weakening: { tone: "warn", fill: "var(--warn)", label: "Weakening: strong but fading" },
  Lagging: { tone: "neg", fill: "var(--neg)", label: "Lagging: weak and fading" },
};

const SIZE = 520;
const MARGIN = 44;
const PLOT = SIZE - MARGIN * 2;

export default function Rrg() {
  const { config } = useConfig();
  const { data } = useTopic<RrgTopic>(config ? `rrg:${config.underlying}` : null);
  const points = data?.rrg?.data_points ?? [];
  const sorted = useMemo(() => [...points].sort((a, b) => a.strike - b.strike || a.option_type.localeCompare(b.option_type)), [points]);

  return (
    <>
      <PageHeader title="Relative Rotation Graph" description="How each contract's strength and momentum compare to the rest, around the 100 line." />
      <Card flush>
        {points.length === 0 ? (
          <EmptyState title="RRG is not available" icon={<Orbit className="size-5" />}>
            {data && data.rrg === null ? "RRG is switched off on this server (enable_rrg in config.yaml)." : "Waiting for enough ticks to calculate it."}
          </EmptyState>
        ) : (
          <div className="grid gap-4 p-4 lg:grid-cols-[minmax(0,34rem)_1fr] lg:gap-6 lg:p-5">
            <RrgPlot points={points} />
            <TableWrap label="RRG values" className="max-h-136">
              <table className={TABLE}>
                <caption className="sr-only">Relative rotation values for each contract</caption>
                <thead>
                  <tr>
                    <th className={cn(TH, R)}>Strike</th>
                    <th className={TH}>Type</th>
                    <th className={cn(TH, R)}>RS</th>
                    <th className={cn(TH, R)}>Ratio</th>
                    <th className={cn(TH, R)}>Momentum</th>
                    <th className={TH}>Quadrant</th>
                  </tr>
                </thead>
                <tbody>
                  {sorted.map((p) => (
                    <tr key={`${p.strike}-${p.option_type}`}>
                      <td className={cn(TD, R)}>{fmtInt(p.strike)}</td>
                      <td className={TD}>{p.option_type}</td>
                      <td className={cn(TD, R)}>{fmt(p.rs)}</td>
                      <td className={cn(TD, R)}>{fmt(p.rs_ratio)}</td>
                      <td className={cn(TD, R)}>{fmt(p.rs_momentum)}</td>
                      <td className={TD}>
                        <Badge tone={QUADRANT[p.quadrant]?.tone ?? "neutral"}>{p.quadrant}</Badge>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </TableWrap>
          </div>
        )}
      </Card>
    </>
  );
}

function RrgPlot({ points }: { points: RrgPoint[] }) {
  const span = Math.max(10, ...points.map((p) => Math.abs(p.rs_ratio - 100)), ...points.map((p) => Math.abs(p.rs_momentum - 100)), 1) * 1.25;
  const toX = (v: number) => MARGIN + ((v - (100 - span)) / (span * 2)) * PLOT;
  const toY = (v: number) => MARGIN + PLOT - ((v - (100 - span)) / (span * 2)) * PLOT;
  const cx = toX(100);
  const cy = toY(100);
  const counts = Object.keys(QUADRANT).map((q) => `${points.filter((p) => p.quadrant === q).length} ${q.toLowerCase()}`).join(", ");
  return (
    <svg viewBox={`0 0 ${SIZE} ${SIZE}`} role="img" aria-label={`Relative rotation scatter plot of ${points.length} contracts: ${counts}. The same data is in the table.`} className="h-auto w-full max-w-xl">
      <rect x={cx} y={MARGIN} width={MARGIN + PLOT - cx} height={cy - MARGIN} fill={QUADRANT.Leading!.fill} opacity="0.1" />
      <rect x={MARGIN} y={MARGIN} width={cx - MARGIN} height={cy - MARGIN} fill={QUADRANT.Improving!.fill} opacity="0.1" />
      <rect x={MARGIN} y={cy} width={cx - MARGIN} height={MARGIN + PLOT - cy} fill={QUADRANT.Lagging!.fill} opacity="0.1" />
      <rect x={cx} y={cy} width={MARGIN + PLOT - cx} height={MARGIN + PLOT - cy} fill={QUADRANT.Weakening!.fill} opacity="0.1" />
      <line x1={MARGIN} y1={cy} x2={MARGIN + PLOT} y2={cy} stroke="var(--control)" />
      <line x1={cx} y1={MARGIN} x2={cx} y2={MARGIN + PLOT} stroke="var(--control)" />
      {/* Quadrant names are drawn as text so they do not rely on colour. */}
      <g fill="var(--muted)" fontSize="12" fontWeight="600">
        <text x={MARGIN + PLOT - 6} y={MARGIN + 16} textAnchor="end">Leading</text>
        <text x={MARGIN + 6} y={MARGIN + 16}>Improving</text>
        <text x={MARGIN + 6} y={MARGIN + PLOT - 8}>Lagging</text>
        <text x={MARGIN + PLOT - 6} y={MARGIN + PLOT - 8} textAnchor="end">Weakening</text>
      </g>
      <text x={MARGIN + PLOT} y={cy - 8} textAnchor="end" fill="var(--subtle)" fontSize="11">RS-Ratio →</text>
      <text x={cx + 8} y={MARGIN + 34} fill="var(--subtle)" fontSize="11">RS-Momentum ↑</text>
      {points.map((p) => (
        <g key={`${p.strike}-${p.option_type}`}>
          <circle cx={toX(p.rs_ratio)} cy={toY(p.rs_momentum)} r={p.option_type === "CE" ? 6 : 5} fill={QUADRANT[p.quadrant]?.fill ?? "var(--muted)"} stroke="var(--surface)" strokeWidth="1.5">
            <title>{`${p.option_type} ${p.strike}: ratio ${fmt(p.rs_ratio)}, momentum ${fmt(p.rs_momentum)}, ${QUADRANT[p.quadrant]?.label ?? p.quadrant}`}</title>
          </circle>
          <text x={toX(p.rs_ratio)} y={toY(p.rs_momentum) - 9} textAnchor="middle" fill="var(--muted)" fontSize="10">
            {fmtInt(p.strike)}{p.option_type[0]}
          </text>
        </g>
      ))}
    </svg>
  );
}
