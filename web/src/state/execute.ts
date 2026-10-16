import type { PaperPosition } from "../api/types";
import type { Selected } from "./selection";

export interface BatchResult {
  succeeded: Selected[];
  failed: { entry: Selected; message: string }[];
}

type Open = (entry: Selected) => Promise<PaperPosition>;

/**
 * Open every selected contract as its own independent simulated position, all at once.
 * One failing contract never stops the others; the caller removes only the successes from the
 * selection so a failed one stays put for the user to fix or retry.
 */
export async function executeEntries(entries: readonly Selected[], open: Open): Promise<BatchResult> {
  const settled = await Promise.allSettled(entries.map((e) => open(e)));
  const result: BatchResult = { succeeded: [], failed: [] };
  settled.forEach((outcome, i) => {
    const entry = entries[i]!;
    if (outcome.status === "fulfilled") result.succeeded.push(entry);
    else {
      const reason = outcome.reason as unknown;
      result.failed.push({ entry, message: reason instanceof Error ? reason.message : `Could not open ${entry.strike} ${entry.optionType}` });
    }
  });
  return result;
}

export function summarise(result: BatchResult): { ok: boolean; text: string } {
  const { succeeded, failed } = result;
  if (failed.length === 0) {
    return { ok: true, text: `Executed ${succeeded.length} paper position${succeeded.length === 1 ? "" : "s"}, each independent.` };
  }
  const first = failed[0]!;
  return { ok: false, text: `${succeeded.length} executed, ${failed.length} failed (${first.entry.strike} ${first.entry.optionType}: ${first.message})` };
}
