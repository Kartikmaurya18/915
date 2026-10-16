import { createContext, useCallback, useContext, useEffect, useRef, useSyncExternalStore, type ReactNode } from "react";
import { getAccessToken, refreshOnce } from "../api/http";
import { useAuth } from "../auth/AuthContext";
import { RealtimeClient, type ConnectionInfo, type TopicState } from "./client";

const RealtimeContext = createContext<RealtimeClient | null>(null);

function wsUrl(): string {
  const scheme = window.location.protocol === "https:" ? "wss" : "ws";
  return `${scheme}://${window.location.host}/ws/v1`;
}

export function RealtimeProvider({ children, client: injected }: { children: ReactNode; client?: RealtimeClient }) {
  const { status, mode, tokenVersion } = useAuth();
  const modeRef = useRef(mode);
  modeRef.current = mode;
  const clientRef = useRef<RealtimeClient | null>(injected ?? null);
  if (!clientRef.current) {
    clientRef.current = new RealtimeClient({
      url: wsUrl,
      authRequired: () => modeRef.current === "required",
      getToken: getAccessToken,
      onAuthFailure: refreshOnce,
    });
  }
  const client = clientRef.current;

  // Connect only for a signed-in session; tear everything down on logout.
  useEffect(() => {
    if (status !== "authenticated" || mode === null) return;
    client.start();
    return () => client.stop();
  }, [client, status, mode]);

  // A newly issued access token is handed to the open socket so it outlives the old expiry.
  useEffect(() => {
    if (status === "authenticated") client.renewToken();
  }, [client, status, tokenVersion]);

  return <RealtimeContext.Provider value={client}>{children}</RealtimeContext.Provider>;
}

export function useRealtimeClient(): RealtimeClient {
  const client = useContext(RealtimeContext);
  if (!client) throw new Error("useRealtimeClient must be used inside <RealtimeProvider>");
  return client;
}

const NO_STATE: TopicState | undefined = undefined;

/** Latest state of one topic (undefined until the first snapshot). Re-renders on every update. */
export function useTopic<T>(topic: string | null): { data: T | undefined; state: TopicState | undefined } {
  const client = useRealtimeClient();
  const subscribe = useCallback(
    (onChange: () => void) => (topic ? client.subscribe(topic, onChange) : () => undefined),
    [client, topic],
  );
  const state = useSyncExternalStore(
    subscribe,
    () => (topic ? client.getTopic(topic) : NO_STATE),
    () => NO_STATE,
  );
  return { data: state?.data as T | undefined, state };
}

export function useConnection(): ConnectionInfo {
  const client = useRealtimeClient();
  const subscribe = useCallback((onChange: () => void) => client.onInfo(onChange), [client]);
  return useSyncExternalStore(subscribe, () => client.getInfo(), () => client.getInfo());
}
