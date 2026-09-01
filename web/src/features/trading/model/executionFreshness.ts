export const EXECUTION_HEARTBEAT_FRESH_MS = 5_000;

export function currentExecutionHeartbeat({
  heartbeatAtNs,
  measuredAtMs,
  nowMs,
  queryHealthy,
}: {
  heartbeatAtNs: number | null;
  measuredAtMs: number;
  nowMs: number;
  queryHealthy: boolean;
}): boolean {
  if (heartbeatAtNs == null) return false;
  const heartbeatAtMs = Math.trunc(heartbeatAtNs / 1_000_000);
  const serverAgeMs = measuredAtMs - heartbeatAtMs;
  const cacheAgeMs = nowMs - measuredAtMs;
  return (
    queryHealthy &&
    heartbeatAtMs > 0 &&
    serverAgeMs >= 0 &&
    cacheAgeMs >= 0 &&
    serverAgeMs + cacheAgeMs <= EXECUTION_HEARTBEAT_FRESH_MS
  );
}
