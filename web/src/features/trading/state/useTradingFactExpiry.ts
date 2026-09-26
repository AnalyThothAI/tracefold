import { useEffect, useReducer, useRef } from "react";

/** Spend a server-computed freshness budget using the browser's monotonic clock. */
export function useTradingFactExpiry(
  expiresAtMs: number | null | undefined,
  remainingMs: number | null | undefined,
): boolean {
  const [, tick] = useReducer((value: number) => value + 1, 0);
  const deadline = useRef<{ key: number; at: number } | null>(null);
  if (expiresAtMs == null || remainingMs == null) {
    deadline.current = null;
  } else if (deadline.current?.key !== expiresAtMs) {
    // A repeat of the same stored heartbeat cannot renew its budget.
    deadline.current = { key: expiresAtMs, at: performance.now() + Math.max(0, remainingMs) };
  }

  useEffect(() => {
    const current = deadline.current;
    if (current == null) return;
    const delay = current.at - performance.now();
    if (delay <= 0) return;
    const timer = setTimeout(tick, Math.min(delay + 1, 2_147_483_647));
    return () => clearTimeout(timer);
  }, [expiresAtMs, remainingMs]);

  useEffect(() => {
    document.addEventListener("visibilitychange", tick);
    return () => document.removeEventListener("visibilitychange", tick);
  }, []);
  return deadline.current != null && performance.now() >= deadline.current.at;
}
