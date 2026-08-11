import type { components } from "@lib/types/openapi";

export const TOKEN_RADAR_SNAPSHOT_SCHEMA = "token_radar_snapshot_v2" as const;

export type TokenRadarSnapshotItem = components["schemas"]["TokenRadarItemData"];
export type TokenRadarSnapshot = components["schemas"]["TokenRadarData"];

export function parseTokenRadarSnapshot(value: unknown): TokenRadarSnapshot {
  const snapshot = record(value, "snapshot");
  exactKeys(
    snapshot,
    ["schema_version", "evidence_as_of_ms", "eligible_total", "items"],
    "snapshot",
  );
  if (snapshot.schema_version !== TOKEN_RADAR_SNAPSHOT_SCHEMA) fail("snapshot.schema_version");
  const evidenceAsOfMs = nonnegativeInteger(
    snapshot.evidence_as_of_ms,
    "snapshot.evidence_as_of_ms",
  );
  const eligibleTotal = nonnegativeInteger(snapshot.eligible_total, "snapshot.eligible_total");
  if (!Array.isArray(snapshot.items) || snapshot.items.length > 50) fail("snapshot.items");
  const items = snapshot.items.map(parseItem);
  if (eligibleTotal < items.length) fail("snapshot.eligible_total");
  if (
    items.some(
      (item) => item.market.observed_at_ms !== null && item.market.observed_at_ms > evidenceAsOfMs,
    )
  ) {
    fail("snapshot.evidence_as_of_ms");
  }
  return {
    schema_version: TOKEN_RADAR_SNAPSHOT_SCHEMA,
    evidence_as_of_ms: evidenceAsOfMs,
    eligible_total: eligibleTotal,
    items,
  };
}

function parseItem(value: unknown, index: number): TokenRadarSnapshotItem {
  const path = `snapshot.items[${index}]`;
  const item = record(value, path);
  exactKeys(
    item,
    [
      "target",
      "trigger_event_id",
      "triggered_at_ms",
      "why_now",
      "evidence",
      "market",
      "counter_evidence",
    ],
    path,
  );
  const target = record(item.target, `${path}.target`);
  exactKeys(
    target,
    ["target_type", "target_id", "symbol", "name", "logo_url", "chain", "exchange", "address"],
    `${path}.target`,
  );
  if (target.target_type !== "Asset" && target.target_type !== "CexToken")
    fail(`${path}.target.target_type`);
  const chain = nullableString(target.chain, `${path}.target.chain`);
  const exchange = nullableString(target.exchange, `${path}.target.exchange`);
  const address = nullableString(target.address, `${path}.target.address`);
  const assetIdentityValid =
    target.target_type === "Asset" && chain !== null && address !== null && exchange === null;
  const cexIdentityValid =
    target.target_type === "CexToken" && exchange !== null && chain === null && address === null;
  if (!assetIdentityValid && !cexIdentityValid) fail(`${path}.target`);
  const whyNow = record(item.why_now, `${path}.why_now`);
  exactKeys(whyNow, ["current_mentions", "prior_mentions", "mention_delta"], `${path}.why_now`);
  const currentMentions = nonnegativeInteger(
    whyNow.current_mentions,
    `${path}.why_now.current_mentions`,
  );
  const priorMentions = nonnegativeInteger(whyNow.prior_mentions, `${path}.why_now.prior_mentions`);
  const mentionDelta = nonnegativeInteger(whyNow.mention_delta, `${path}.why_now.mention_delta`);
  if (currentMentions - priorMentions !== mentionDelta) fail(`${path}.why_now.mention_delta`);
  const evidence = record(item.evidence, `${path}.evidence`);
  exactKeys(
    evidence,
    [
      "new_independent_author_count",
      "independent_text_count",
      "time_to_nth_author_ms",
      "duplicate_share",
    ],
    `${path}.evidence`,
  );
  const duplicateShare = finiteNumber(evidence.duplicate_share, `${path}.evidence.duplicate_share`);
  if (duplicateShare < 0 || duplicateShare > 1) fail(`${path}.evidence.duplicate_share`);
  const market = record(item.market, `${path}.market`);
  exactKeys(
    market,
    ["status", "price_usd", "price_change_since_signal", "market_cap_usd", "observed_at_ms"],
    `${path}.market`,
  );
  if (market.status !== "confirmed" && market.status !== "unavailable")
    fail(`${path}.market.status`);
  const priceChange = nullableFiniteNumber(
    market.price_change_since_signal,
    `${path}.market.price_change_since_signal`,
  );
  const priceUsd = nullablePositiveNumber(market.price_usd, `${path}.market.price_usd`);
  const marketCapUsd = nullablePositiveNumber(
    market.market_cap_usd,
    `${path}.market.market_cap_usd`,
  );
  const observedAtMs = nullableNonnegativeInteger(
    market.observed_at_ms,
    `${path}.market.observed_at_ms`,
  );
  const hasMetrics = priceUsd !== null || marketCapUsd !== null;
  if (hasMetrics !== (observedAtMs !== null)) fail(`${path}.market.observed_at_ms`);
  if (market.status === "unavailable" && priceChange !== null) {
    fail(`${path}.market.price_change_since_signal`);
  }
  if (
    market.status === "confirmed" &&
    (priceChange === null || priceUsd === null || observedAtMs === null)
  ) {
    fail(`${path}.market.price_change_since_signal`);
  }
  return {
    target: {
      target_type: target.target_type,
      target_id: nonemptyString(target.target_id, `${path}.target.target_id`),
      symbol: nonemptyString(target.symbol, `${path}.target.symbol`),
      name: nullableString(target.name, `${path}.target.name`),
      logo_url: nullableTokenImagePath(target.logo_url, `${path}.target.logo_url`),
      chain,
      exchange,
      address,
    },
    trigger_event_id: nonemptyString(item.trigger_event_id, `${path}.trigger_event_id`),
    triggered_at_ms: nonnegativeInteger(item.triggered_at_ms, `${path}.triggered_at_ms`),
    why_now: {
      current_mentions: currentMentions,
      prior_mentions: priorMentions,
      mention_delta: mentionDelta,
    },
    evidence: {
      new_independent_author_count: nonnegativeInteger(
        evidence.new_independent_author_count,
        `${path}.evidence.new_independent_author_count`,
      ),
      independent_text_count: nonnegativeInteger(
        evidence.independent_text_count,
        `${path}.evidence.independent_text_count`,
      ),
      time_to_nth_author_ms: nonnegativeInteger(
        evidence.time_to_nth_author_ms,
        `${path}.evidence.time_to_nth_author_ms`,
      ),
      duplicate_share: duplicateShare,
    },
    market: {
      status: market.status,
      price_usd: priceUsd,
      price_change_since_signal: priceChange,
      market_cap_usd: marketCapUsd,
      observed_at_ms: observedAtMs,
    },
    counter_evidence: counterEvidence(item.counter_evidence, `${path}.counter_evidence`),
  };
}

function record(value: unknown, path: string): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) fail(path);
  return value as Record<string, unknown>;
}

function exactKeys(value: Record<string, unknown>, keys: string[], path: string): void {
  const expected = new Set(keys);
  const actual = Object.keys(value);
  if (actual.length !== keys.length || actual.some((key) => !expected.has(key))) fail(path);
}

function nonemptyString(value: unknown, path: string): string {
  if (typeof value !== "string" || !value.trim()) fail(path);
  return value;
}

function nullableString(value: unknown, path: string): string | null {
  return value === null ? null : nonemptyString(value, path);
}

function nullableTokenImagePath(value: unknown, path: string): string | null {
  const parsed = nullableString(value, path);
  if (parsed !== null && !/^\/api\/token-images\/[0-9a-f]{64}$/.test(parsed)) fail(path);
  return parsed;
}

function counterEvidence(value: unknown, path: string): "market_confirmation_unavailable" | null {
  if (value === null || value === "market_confirmation_unavailable") return value;
  return fail(path);
}

function finiteNumber(value: unknown, path: string): number {
  if (typeof value !== "number" || !Number.isFinite(value)) fail(path);
  return value;
}

function nullableFiniteNumber(value: unknown, path: string): number | null {
  return value === null ? null : finiteNumber(value, path);
}

function nullablePositiveNumber(value: unknown, path: string): number | null {
  const parsed = nullableFiniteNumber(value, path);
  if (parsed !== null && parsed <= 0) fail(path);
  return parsed;
}

function integer(value: unknown, path: string): number {
  const parsed = finiteNumber(value, path);
  if (!Number.isInteger(parsed)) fail(path);
  return parsed;
}

function nonnegativeInteger(value: unknown, path: string): number {
  const parsed = integer(value, path);
  if (parsed < 0) fail(path);
  return parsed;
}

function nullableNonnegativeInteger(value: unknown, path: string): number | null {
  return value === null ? null : nonnegativeInteger(value, path);
}

function fail(path: string): never {
  throw new Error(`token_radar_snapshot_contract:${path}`);
}
