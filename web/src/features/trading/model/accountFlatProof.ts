export const ACCOUNT_FLAT_PROOF_FRESH_MS = 10_000;

export function currentAccountFlatProof({
  accountFlatProven,
  measuredAtMs,
  nowMs,
  queryHealthy,
}: {
  accountFlatProven: boolean;
  measuredAtMs: number;
  nowMs: number;
  queryHealthy: boolean;
}): boolean {
  const ageMs = nowMs - measuredAtMs;
  return (
    accountFlatProven &&
    queryHealthy &&
    measuredAtMs > 0 &&
    ageMs >= 0 &&
    ageMs <= ACCOUNT_FLAT_PROOF_FRESH_MS
  );
}
