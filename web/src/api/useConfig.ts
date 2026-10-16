import { useEffect, useState } from "react";
import { api } from "./http";
import type { AppConfig } from "./types";

/** Server-side, non-secret settings (underlying, candle intervals). Fetched once per signed-in session. */
export function useConfig(): { config: AppConfig | null; error: string | null } {
  const [config, setConfig] = useState<AppConfig | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    const ctrl = new AbortController();
    api
      .get<AppConfig>("/api/config", ctrl.signal)
      .then(setConfig)
      .catch((err: unknown) => {
        if (!ctrl.signal.aborted) setError(err instanceof Error ? err.message : "Could not load configuration");
      });
    return () => ctrl.abort();
  }, []);
  return { config, error };
}
