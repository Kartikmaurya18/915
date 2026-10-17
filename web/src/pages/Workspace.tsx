import { memo, useState } from "react";
import { useConfig } from "../api/useConfig";
import type { IndexTopic, OptionsTopic } from "../api/types";
import { PriceChartCard } from "../components/PriceChartCard";
import { StraddleChart } from "../components/StraddleChart";
import { StrangleChart } from "../components/StrangleChart";
import { WatchlistPanel } from "../components/WatchlistPanel";
import { LAYOUTS, PANE_KINDS, gridClass, loadWorkspace, paneCount, saveWorkspace, type LayoutId, type PaneKind, type WorkspaceState } from "../lib/workspace";
import { useTopic } from "../realtime/RealtimeProvider";
import { ErrorState } from "../ui/feedback";
import { Select } from "../ui/fields";
import { PageHeader } from "../ui/layout";
import { Segmented } from "../ui/Segmented";

export default function Workspace() {
  const { config, error } = useConfig();
  const [ws, setWs] = useState<WorkspaceState>(loadWorkspace);

  function change(next: WorkspaceState) {
    setWs(next);
    saveWorkspace(next);
  }
  if (error) return <ErrorState>{error}</ErrorState>;
  const shown = ws.panes.slice(0, paneCount(ws.layout));

  return (
    <>
      <PageHeader
        title="Workspace"
        description="Arrange several charts side by side and keep a watchlist beside them. Your layout is remembered on this device."
        actions={
          <Segmented
            label="Number of charts"
            size="sm"
            value={ws.layout}
            onChange={(v: LayoutId) => change({ ...ws, layout: v })}
            options={LAYOUTS.map((l) => ({ value: l.value, label: l.label, title: l.title }))}
          />
        }
      />
      <div className={`grid gap-4 ${gridClass(ws.layout)}`}>
        {shown.map((kind, i) => (
          <Pane key={i} index={i} kind={kind} underlying={config?.underlying ?? null} intervals={config?.candle_intervals ?? [1, 5, 15]} onKind={(k) => change({ ...ws, panes: ws.panes.map((p, j) => (j === i ? k : p)) })} />
        ))}
      </div>
      <WatchlistPanel />
    </>
  );
}

interface PaneProps {
  index: number;
  kind: PaneKind;
  underlying: string | null;
  intervals: number[];
  onKind: (k: PaneKind) => void;
}

/** One chart slot: a chart-type chooser above the chart itself. Memoised so a tick in one pane does not redraw the others. */
const Pane = memo(function Pane({ index, kind, underlying, intervals, onKind }: PaneProps) {
  return (
    <section aria-label={`Chart ${index + 1}`} className="grid min-w-0 content-start gap-2">
      <label className="flex items-center gap-2 text-sm text-muted">
        Chart {index + 1}
        <Select value={kind} onChange={(e) => onKind(e.target.value as PaneKind)} className="min-h-8 w-auto py-1">
          {PANE_KINDS.map((k) => <option key={k.value} value={k.value}>{k.label}</option>)}
        </Select>
      </label>
      {underlying === null ? null : kind === "price" ? (
        <PriceChartCard underlying={underlying} intervals={intervals} />
      ) : kind === "straddle" ? (
        <StraddlePane underlying={underlying} />
      ) : (
        <StranglePane underlying={underlying} />
      )}
    </section>
  );
});

function StraddlePane({ underlying }: { underlying: string }) {
  const options = useTopic<OptionsTopic>(`options:${underlying}`).data;
  const index = useTopic<IndexTopic>(`index:${underlying}`).data;
  const [picked, setPicked] = useState<number | null>(null);
  const atm = index?.atm?.strike ?? null;
  const strike = picked ?? atm; // follows the money until the user chooses a strike
  const row = strike === null ? null : (options?.strikes.find((r) => r.strike === strike) ?? null);
  const list = (options?.strikes ?? []).map((r) => r.strike).sort((a, b) => a - b);
  return <StraddleChart strike={strike} liveRow={row} expiry={options?.expiry ?? null} chip={picked === null ? "Following ATM" : undefined} options={list} onPick={setPicked} />;
}

function StranglePane({ underlying }: { underlying: string }) {
  const options = useTopic<OptionsTopic>(`options:${underlying}`).data;
  const index = useTopic<IndexTopic>(`index:${underlying}`).data;
  return <StrangleChart rows={options?.strikes ?? []} atm={index?.atm?.strike ?? null} expiry={options?.expiry ?? null} />;
}
