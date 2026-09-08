import { useEffect, useReducer } from "react";

/** Re-render exactly when the server's published fact budget expires, including failed polls. */
export function useTradingFactExpiry(expiresAtMs: number | null | undefined): boolean {
  const [generation, tick] = useReducer((value: number) => value + 1, 0);
  useEffect(() => {
    if (expiresAtMs == null) return;
    const delay = expiresAtMs - Date.now() + 1;
    if (delay <= 0) return;
    const timer = setTimeout(tick, Math.min(delay, 2_147_483_647));
    return () => clearTimeout(timer);
  }, [expiresAtMs, generation]);
  return expiresAtMs != null && Date.now() > expiresAtMs;
}
