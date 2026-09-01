export const ACCOUNT_FLAT_PROOF_FRESH_MS = 10_000;

export function currentAccountFlatProof({
  accountFlatProven,
  measuredAtMs,
  nowMs,
  queryHealthy,
  reconciliationAgeMs,
}: {
  accountFlatProven: boolean;
  measuredAtMs: number;
  nowMs: number;
  queryHealthy: boolean;
  reconciliationAgeMs: number | null;
}): boolean {
  const cacheAgeMs = nowMs - measuredAtMs;
  return (
    accountFlatProven &&
    queryHealthy &&
    measuredAtMs > 0 &&
    cacheAgeMs >= 0 &&
    reconciliationAgeMs != null &&
    reconciliationAgeMs >= 0 &&
    cacheAgeMs + reconciliationAgeMs <= ACCOUNT_FLAT_PROOF_FRESH_MS
  );
}
