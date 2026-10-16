import { useConfig } from "../api/useConfig";
import type { IndexTopic } from "../api/types";
import { fmtAge, fmtInt, fmtTime } from "../lib/format";
import { useConnection, useTopic } from "../realtime/RealtimeProvider";

export default function Quality() {
  const { config } = useConfig();
  const { data, state } = useTopic<IndexTopic>(config ? `index:${config.underlying}` : null);
  const conn = useConnection();
  const dq = data?.data_quality;
  // End-to-end latency: ingestor publish time -> now. Includes clock skew between server and browser.
  const latency = state ? Math.max(0, Date.now() - state.pub) : null;

  return (
    <div className="grid-2">
      <section className="panel">
        <header className="panel-head"><h2>Market data</h2></header>
        <dl className="kv">
          <Row k="Source" v={dq?.source} />
          <Row k="Connection" v={dq?.connection_status} />
          <Row k="Market session" v={dq?.market_session} />
          <Row k="Last tick" v={dq ? fmtTime(dq.last_tick_time) : undefined} />
          <Row k="Last tick age" v={dq?.last_tick_age_seconds != null ? `${dq.last_tick_age_seconds.toFixed(1)} s` : undefined} />
          <Row k="Stale" v={dq ? (dq.is_stale ? `Yes (over ${dq.stale_threshold_seconds}s)` : "No") : undefined} bad={dq?.is_stale} />
          <Row k="Ticks received" v={dq ? fmtInt(dq.tick_count) : undefined} />
          <Row k="Duplicate ticks" v={dq ? fmtInt(dq.duplicate_tick_count) : undefined} />
          <Row k="Rejected ticks" v={dq ? fmtInt(dq.rejected_tick_count) : undefined} />
          <Row k="History persisted" v={dq ? (dq.persistence_enabled ? "Yes" : "No") : undefined} />
        </dl>
      </section>
      <section className="panel">
        <header className="panel-head"><h2>Your connection</h2></header>
        <dl className="kv">
          <Row k="Status" v={conn.status} />
          <Row k="Gateway feed age" v={fmtAge(conn.feedAgeMs)} />
          <Row k="Publish → browser" v={latency === null ? undefined : fmtAge(latency)} />
          <Row k="Snapshots / patches" v={`${conn.snapshots} / ${conn.patches}`} />
          <Row k="Resyncs" v={String(conn.resyncs)} />
          <Row k="Reconnects" v={String(conn.reconnects)} />
          <Row k="Data received" v={`${(conn.bytes / 1024).toFixed(1)} KB`} />
        </dl>
      </section>
    </div>
  );
}

function Row({ k, v, bad }: { k: string; v: string | undefined; bad?: boolean }) {
  return (
    <>
      <dt>{k}</dt>
      <dd className={bad ? "neg" : undefined}>{v ?? "—"}</dd>
    </>
  );
}
