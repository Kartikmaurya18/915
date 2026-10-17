import type { ReactNode } from "react";
import { useConfig } from "../api/useConfig";
import type { IndexTopic } from "../api/types";
import { fmtAge, fmtInt, fmtTime } from "../lib/format";
import { useConnection, useTopic } from "../realtime/RealtimeProvider";
import { Badge } from "../ui/Badge";
import { Card } from "../ui/Card";
import { Skeleton } from "../ui/feedback";
import { PageHeader } from "../ui/layout";

export default function Quality() {
  const { config } = useConfig();
  const { data, state } = useTopic<IndexTopic>(config ? `index:${config.underlying}` : null);
  const conn = useConnection();
  const dq = data?.data_quality;
  // End-to-end latency: ingestor publish time -> now. Includes clock skew between server and browser.
  const latency = state ? Math.max(0, Date.now() - state.pub) : null;

  return (
    <>
      <PageHeader title="Data quality" description="How healthy the market feed and your connection are right now." />
      <div className="grid gap-4 lg:grid-cols-2 lg:gap-5">
        <Card title="Market data" actions={dq && <Badge tone={dq.is_stale ? "warn" : "pos"}>{dq.is_stale ? "Stale" : "Fresh"}</Badge>}>
          <dl className="grid grid-cols-[max-content_1fr] gap-x-6 gap-y-2.5 text-sm">
            <Row k="Source" v={dq?.source} loading={!dq} />
            <Row k="Connection" v={dq?.connection_status} loading={!dq} />
            <Row k="Market session" v={dq?.market_session} loading={!dq} />
            <Row k="Last tick" v={dq ? fmtTime(dq.last_tick_time) : undefined} loading={!dq} />
            <Row k="Last tick age" v={dq?.last_tick_age_seconds != null ? `${dq.last_tick_age_seconds.toFixed(1)} s` : undefined} loading={!dq} />
            <Row k="Stale" v={dq ? (dq.is_stale ? `Yes (over ${dq.stale_threshold_seconds}s)` : "No") : undefined} loading={!dq} bad={dq?.is_stale} />
            <Row k="Ticks received" v={dq ? fmtInt(dq.tick_count) : undefined} loading={!dq} />
            <Row k="Duplicate ticks" v={dq ? fmtInt(dq.duplicate_tick_count) : undefined} loading={!dq} />
            <Row k="Rejected ticks" v={dq ? fmtInt(dq.rejected_tick_count) : undefined} loading={!dq} />
            <Row k="History persisted" v={dq ? (dq.persistence_enabled ? "Yes" : "No") : undefined} loading={!dq} />
          </dl>
        </Card>
        <Card title="Your connection">
          <dl className="grid grid-cols-[max-content_1fr] gap-x-6 gap-y-2.5 text-sm">
            <Row k="Status" v={conn.status} />
            <Row k="Gateway feed age" v={fmtAge(conn.feedAgeMs)} />
            <Row k="Publish → browser" v={latency === null ? undefined : fmtAge(latency)} />
            <Row k="Snapshots / patches" v={`${conn.snapshots} / ${conn.patches}`} />
            <Row k="Resyncs" v={String(conn.resyncs)} />
            <Row k="Reconnects" v={String(conn.reconnects)} />
            <Row k="Data received" v={`${(conn.bytes / 1024).toFixed(1)} KB`} />
          </dl>
        </Card>
      </div>
    </>
  );
}

function Row({ k, v, bad, loading }: { k: string; v: ReactNode | undefined; bad?: boolean; loading?: boolean }) {
  return (
    <>
      <dt className="text-muted">{k}</dt>
      <dd className={bad ? "num text-right font-medium text-warn" : "num text-right font-medium"}>{loading ? <Skeleton className="ml-auto h-4 w-20" /> : (v ?? "—")}</dd>
    </>
  );
}
