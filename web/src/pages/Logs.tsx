import { useCallback, useEffect, useState } from "react";
import { RefreshCw, ScrollText } from "lucide-react";
import { api } from "../api/http";
import type { LogEntry } from "../api/types";
import { fmtTime } from "../lib/format";
import { Badge } from "../ui/Badge";
import { Button } from "../ui/Button";
import { Card } from "../ui/Card";
import { EmptyState, ErrorState } from "../ui/feedback";
import { Checkbox, Select } from "../ui/fields";
import { PageHeader, TABLE, TD, TH, TableWrap } from "../ui/layout";

const LEVELS = ["", "INFO", "WARNING", "ERROR"] as const;
const TONE = { error: "neg", warning: "warn", info: "neutral" } as const;

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
    <>
      <PageHeader title="System logs" description="The latest events from the market engine. Administrators only." />
      <Card
        flush
        actions={
          <>
            <Select aria-label="Level" value={level} onChange={(e) => setLevel(e.target.value as (typeof LEVELS)[number])} className="min-h-8 w-auto py-1">
              {LEVELS.map((l) => (
                <option key={l} value={l}>
                  {l || "All levels"}
                </option>
              ))}
            </Select>
            <label className="flex items-center gap-2 text-sm text-muted">
              <Checkbox checked={auto} onChange={(e) => setAuto(e.target.checked)} />
              Auto-refresh
            </label>
            <Button size="sm" onClick={() => void load()}>
              <RefreshCw aria-hidden="true" className="size-3.5" />
              Refresh
            </Button>
          </>
        }
      >
        <div className="px-2 pb-3 sm:px-3">
          {error && <ErrorState className="mx-2 mb-3">{error}</ErrorState>}
          {logs?.length === 0 ? (
            <EmptyState title="No log entries" icon={<ScrollText className="size-5" />} />
          ) : (
            <TableWrap label="System logs" className="max-h-144">
              <table className={TABLE}>
                <caption className="sr-only">System log entries, newest first</caption>
                <thead>
                  <tr>
                    <th className={TH}>Time</th>
                    <th className={TH}>Level</th>
                    <th className={TH}>Event</th>
                    <th className={TH}>Detail</th>
                  </tr>
                </thead>
                <tbody>
                  {logs?.map((e, i) => {
                    const lvl = (e.level ?? "info").toLowerCase() as keyof typeof TONE;
                    return (
                      <tr key={`${e.timestamp}-${i}`}>
                        <td className={TD}>{fmtTime(e.timestamp)}</td>
                        <td className={TD}>
                          <Badge tone={TONE[lvl] ?? "neutral"}>{e.level ?? "INFO"}</Badge>
                        </td>
                        <td className={TD}>{e.event ?? e.event_type}</td>
                        <td className="min-w-60 whitespace-normal wrap-break-word border-b border-line/70 px-3 py-2 text-muted">{detail(e)}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </TableWrap>
          )}
        </div>
      </Card>
    </>
  );
}
