import { useMemo } from "react";
import { useConfig } from "../api/useConfig";
import type { RrgPoint, RrgTopic } from "../api/types";
import { fmt, fmtInt } from "../lib/format";
import { useTopic } from "../realtime/RealtimeProvider";

const QUADRANT_CLASS: Record<string, string> = {
  Leading: "q-leading",
  Weakening: "q-weakening",
  Lagging: "q-lagging",
  Improving: "q-improving",
};

const SIZE = 520;
const MARGIN = 40;
const PLOT = SIZE - MARGIN * 2;

export default function Rrg() {
  const { config } = useConfig();
  const { data } = useTopic<RrgTopic>(config ? `rrg:${config.underlying}` : null);
  const points = data?.rrg?.data_points ?? [];
  const sorted = useMemo(
    () => [...points].sort((a, b) => a.strike - b.strike || a.option_type.localeCompare(b.option_type)),
    [points],
  );

  return (
    <section className="panel">
      <header className="panel-head">
        <h2>Relative Rotation Graph</h2>
      </header>
      {points.length === 0 ? (
        <p className="empty">
          {data && data.rrg === null
            ? "RRG is switched off on this server (enable_rrg in config.yaml)."
            : "RRG is not available yet — waiting for enough ticks."}
        </p>
      ) : (
        <div className="rrg">
          <RrgPlot points={points} />
          <div className="table-wrap">
            <table className="data">
              <thead>
                <tr>
                  <th className="r">Strike</th>
                  <th>Type</th>
                  <th className="r">RS</th>
                  <th className="r">Ratio</th>
                  <th className="r">Momentum</th>
                  <th>Quadrant</th>
                </tr>
              </thead>
              <tbody>
                {sorted.map((p) => (
                  <tr key={`${p.strike}-${p.option_type}`}>
                    <td className="r">{fmtInt(p.strike)}</td>
                    <td className={p.option_type === "CE" ? "call" : "put"}>{p.option_type}</td>
                    <td className="r">{fmt(p.rs)}</td>
                    <td className="r">{fmt(p.rs_ratio)}</td>
                    <td className="r">{fmt(p.rs_momentum)}</td>
                    <td><span className={`quad ${QUADRANT_CLASS[p.quadrant] ?? ""}`}>{p.quadrant}</span></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </section>
  );
}

function RrgPlot({ points }: { points: RrgPoint[] }) {
  const span =
    Math.max(10, ...points.map((p) => Math.abs(p.rs_ratio - 100)), ...points.map((p) => Math.abs(p.rs_momentum - 100)), 1) * 1.25;
  const toX = (v: number) => MARGIN + ((v - (100 - span)) / (span * 2)) * PLOT;
  const toY = (v: number) => MARGIN + PLOT - ((v - (100 - span)) / (span * 2)) * PLOT;
  const cx = toX(100);
  const cy = toY(100);
  return (
    <svg className="rrg-plot" viewBox={`0 0 ${SIZE} ${SIZE}`} role="img" aria-label="RRG scatter plot">
      <rect x={cx} y={MARGIN} width={MARGIN + PLOT - cx} height={cy - MARGIN} className="bg q-leading" />
      <rect x={MARGIN} y={MARGIN} width={cx - MARGIN} height={cy - MARGIN} className="bg q-improving" />
      <rect x={MARGIN} y={cy} width={cx - MARGIN} height={MARGIN + PLOT - cy} className="bg q-lagging" />
      <rect x={cx} y={cy} width={MARGIN + PLOT - cx} height={MARGIN + PLOT - cy} className="bg q-weakening" />
      <line x1={MARGIN} y1={cy} x2={MARGIN + PLOT} y2={cy} className="axis" />
      <line x1={cx} y1={MARGIN} x2={cx} y2={MARGIN + PLOT} className="axis" />
      <text x={MARGIN + PLOT} y={cy - 8} textAnchor="end" className="axis-label">RS-Ratio →</text>
      <text x={cx + 8} y={MARGIN + 12} className="axis-label">RS-Momentum ↑</text>
      {points.map((p) => (
        <g key={`${p.strike}-${p.option_type}`}>
          <circle cx={toX(p.rs_ratio)} cy={toY(p.rs_momentum)} r={7} className={`pt ${QUADRANT_CLASS[p.quadrant] ?? ""}`}>
            <title>{`${p.option_type} ${p.strike} · Ratio ${fmt(p.rs_ratio)} · Momentum ${fmt(p.rs_momentum)}`}</title>
          </circle>
          <text x={toX(p.rs_ratio)} y={toY(p.rs_momentum) - 10} textAnchor="middle" className="pt-label">
            {fmtInt(p.strike)}{p.option_type[0]}
          </text>
        </g>
      ))}
    </svg>
  );
}
