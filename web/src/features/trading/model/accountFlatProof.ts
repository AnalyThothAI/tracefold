export const ACCOUNT_FLAT_PROOF_FRESH_MS = 10_000;

export function currentReconciliationAge({
  measuredAtMs,
  nowMs,
  reconciliationAgeMs,
}: {
  measuredAtMs: number;
  nowMs: number;
  reconciliationAgeMs: number | null;
}): number | null {
  const cacheAgeMs = nowMs - measuredAtMs;
  if (
    measuredAtMs <= 0 ||
    cacheAgeMs < 0 ||
    reconciliationAgeMs == null ||
    reconciliationAgeMs < 0
  ) {
    return null;
  }
  return reconciliationAgeMs + cacheAgeMs;
}

export function currentPrivateAccountFacts({
  measuredAtMs,
  nowMs,
  queryHealthy,
  reconciliationAgeMs,
}: {
  measuredAtMs: number;
  nowMs: number;
  queryHealthy: boolean;
  reconciliationAgeMs: number | null;
}): boolean {
  const currentAgeMs = currentReconciliationAge({
    measuredAtMs,
    nowMs,
    reconciliationAgeMs,
  });
  return queryHealthy && currentAgeMs != null && currentAgeMs <= ACCOUNT_FLAT_PROOF_FRESH_MS;
}

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
  return (
    accountFlatProven &&
    currentPrivateAccountFacts({
      measuredAtMs,
      nowMs,
      queryHealthy,
      reconciliationAgeMs,
    })
  );
}
