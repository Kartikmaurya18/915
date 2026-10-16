import { useCallback, useEffect, useState } from "react";
import { api } from "../api/http";
import type { LogEntry } from "../api/types";
import { fmtTime } from "../lib/format";

const LEVELS = ["", "INFO", "WARNING", "ERROR"] as const;

function detail(entry: LogEntry): string {
  if (entry.data === undefined || entry.data === null) return "";
  const text = typeof entry.data === "string" ? entry.data : JSON.stringify(entry.data);
  return text.length > 160 ? text.slice(0, 157) + "…" : text;
}

export default function Logs() {
  const [logs, setLogs] = useState<LogEntry[] | null>(null);
  const [level, setLevel] = useState<(typeof LEVELS)[number]>("");
  const [auto, setAuto] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const q = new URLSearchParams({ limit: "200" });
      if (level) q.set("level", level);
      const res = await api.get<{ logs: LogEntry[] }>(`/api/logs?${q}`);
      setLogs(res.logs);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load logs");
    }
  }, [level]);

  useEffect(() => {
    void load();
    if (!auto) return;
    const t = setInterval(() => {
      if (document.visibilityState === "visible") void load();
    }, 5000);
    return () => clearInterval(t);
  }, [load, auto]);

  return (
    <section className="panel">
      <header className="panel-head">
        <h2>System logs</h2>
        <div className="row tight">
          <select aria-label="Level" value={level} onChange={(e) => setLevel(e.target.value as (typeof LEVELS)[number])}>
            {LEVELS.map((l) => (
              <option key={l} value={l}>{l || "All levels"}</option>
            ))}
          </select>
          <label className="check">
            <input type="checkbox" checked={auto} onChange={(e) => setAuto(e.target.checked)} /> Auto-refresh
          </label>
          <button className="btn btn-small" onClick={() => void load()}>Refresh</button>
        </div>
      </header>
      {error && <p className="form-error" role="alert">{error}</p>}
      <div className="table-wrap">
        <table className="data">
          <thead>
            <tr><th>Time</th><th>Level</th><th>Event</th><th>Detail</th></tr>
          </thead>
          <tbody>
            {logs?.length === 0 && (
              <tr><td colSpan={4} className="empty">No log entries</td></tr>
            )}
            {logs?.map((e, i) => (
              <tr key={`${e.timestamp}-${i}`}>
                <td>{fmtTime(e.timestamp)}</td>
                <td><span className={`lvl lvl-${(e.level ?? "info").toLowerCase()}`}>{e.level ?? "INFO"}</span></td>
                <td>{e.event ?? e.event_type}</td>
                <td className="log-detail">{detail(e)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
