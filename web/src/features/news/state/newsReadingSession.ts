import { type QueryClient, useQueryClient } from "@tanstack/react-query";
import { useCallback, useSyncExternalStore, type Dispatch, type SetStateAction } from "react";

const MAX_READING_STATES = 64;
type ReadingSession = {
  values: Map<string, unknown>;
  listeners: Set<() => void>;
};
const sessions = new WeakMap<QueryClient, ReadingSession>();

/** The QueryClient owns this bounded UI session; server records remain only in Query. */
export function newsReadingSession(client: QueryClient): ReadingSession {
  let session = sessions.get(client);
  if (!session) {
    session = { values: new Map(), listeners: new Set() };
    sessions.set(client, session);
  }
  return session;
}

export function readNewsReadingState<T>(session: ReadingSession, key: string, initial: T): T {
  return session.values.has(key) ? (session.values.get(key) as T) : initial;
}

export function writeNewsReadingState<T>(session: ReadingSession, key: string, value: T) {
  if (Object.is(session.values.get(key), value) && session.values.has(key)) return;
  session.values.delete(key);
  session.values.set(key, value);
  if (session.values.size > MAX_READING_STATES) {
    const oldest = session.values.keys().next().value;
    if (oldest !== undefined) session.values.delete(oldest);
  }
  for (const listener of session.listeners) listener();
}

/** Only reading controls belong here: scroll offsets and feed generation. */
export function useNewsReadingState<T>(key: string, initial: T): [T, Dispatch<SetStateAction<T>>] {
  const session = newsReadingSession(useQueryClient());
  const subscribe = useCallback(
    (listener: () => void) => {
      session.listeners.add(listener);
      return () => session.listeners.delete(listener);
    },
    [session],
  );
  const snapshot = useCallback(
    () => readNewsReadingState(session, key, initial),
    [initial, key, session],
  );
  const value = useSyncExternalStore(subscribe, snapshot, snapshot);
  const setValue = useCallback<Dispatch<SetStateAction<T>>>(
    (next) => {
      const current = readNewsReadingState(session, key, initial);
      writeNewsReadingState(
        session,
        key,
        typeof next === "function" ? (next as (value: T) => T)(current) : next,
      );
    },
    [initial, key, session],
  );
  return [value, setValue];
}
