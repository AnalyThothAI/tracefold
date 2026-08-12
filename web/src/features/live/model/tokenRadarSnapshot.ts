import type { components } from "@lib/types/openapi";

export const TOKEN_RADAR_WINDOW = "4h" as const;
export const TOKEN_RADAR_SNAPSHOT_SCHEMA = "token_radar_snapshot_v4" as const;

export type TokenRadarSnapshotItem = components["schemas"]["TokenRadarItemData"];
export type TokenRadarSnapshot = components["schemas"]["TokenRadarData"];

export function parseTokenRadarSnapshot(value: unknown): TokenRadarSnapshot {
  const snapshot = record(value, "snapshot");
  exactKeys(
    snapshot,
    [
      "schema_version",
      "state",
      "stale_reason",
      "state_changed_at_ms",
      "social_evidence_as_of_ms",
      "eligible_total",
      "items",
    ],
    "snapshot",
  );
  if (snapshot.schema_version !== TOKEN_RADAR_SNAPSHOT_SCHEMA) fail("snapshot.schema_version");
  if (
    snapshot.state !== "current" &&
    snapshot.state !== "stale" &&
    snapshot.state !== "unavailable"
  ) {
    fail("snapshot.state");
  }
  const staleReason = tokenRadarStaleReason(snapshot.stale_reason, "snapshot.stale_reason");
  if ((snapshot.state === "stale") !== (staleReason !== null)) {
    fail("snapshot.stale_reason");
  }
  const stateChangedAtMs = nonnegativeInteger(
    snapshot.state_changed_at_ms,
    "snapshot.state_changed_at_ms",
  );
  const socialEvidenceAsOfMs = nonnegativeInteger(
    snapshot.social_evidence_as_of_ms,
    "snapshot.social_evidence_as_of_ms",
  );
  const eligibleTotal = nonnegativeInteger(snapshot.eligible_total, "snapshot.eligible_total");
  if (!Array.isArray(snapshot.items) || snapshot.items.length > 50) fail("snapshot.items");
  const items = snapshot.items.map(parseItem);
  items.forEach((item, index) => {
    if (item.qualified_at_ms > socialEvidenceAsOfMs) {
      fail(`snapshot.items[${index}].qualified_at_ms`);
    }
  });
  if (items.length !== Math.min(eligibleTotal, 50)) fail("snapshot.eligible_total");
  validateServerOrder(items);
  if (
    snapshot.state === "unavailable" &&
    (stateChangedAtMs !== 0 ||
      socialEvidenceAsOfMs !== 0 ||
      eligibleTotal !== 0 ||
      items.length !== 0)
  ) {
    fail("snapshot.state");
  }
  return {
    schema_version: TOKEN_RADAR_SNAPSHOT_SCHEMA,
    state: snapshot.state,
    stale_reason: staleReason,
    state_changed_at_ms: stateChangedAtMs,
    social_evidence_as_of_ms: socialEvidenceAsOfMs,
    eligible_total: eligibleTotal,
    items,
  };
}

function validateServerOrder(items: TokenRadarSnapshotItem[]): void {
  const targets = new Set<string>();
  let previous: TokenRadarSnapshotItem | null = null;
  for (const item of items) {
    const key = `${item.target.target_type}\u0000${item.target.target_id}`;
    if (targets.has(key)) fail("snapshot.items");
    targets.add(key);
    if (
      previous !== null &&
      (item.qualified_at_ms > previous.qualified_at_ms ||
        (item.qualified_at_ms === previous.qualified_at_ms &&
          key < `${previous.target.target_type}\u0000${previous.target.target_id}`))
    ) {
      fail("snapshot.items");
    }
    previous = item;
  }
}

function parseItem(value: unknown, index: number): TokenRadarSnapshotItem {
  const path = `snapshot.items[${index}]`;
  const item = record(value, path);
  exactKeys(
    item,
    [
      "target",
      "trigger_event_id",
      "trigger_source_event_at_ms",
      "qualified_at_ms",
      "why_now",
      "evidence",
      "market",
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
      "independent_author_count",
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
    [
      "price_usd",
      "price_observed_at_ms",
      "price_change_since_signal",
      "market_cap_usd",
      "market_cap_observed_at_ms",
    ],
    `${path}.market`,
  );
  const priceChange = nullableFiniteNumber(
    market.price_change_since_signal,
    `${path}.market.price_change_since_signal`,
  );
  const priceUsd = nullablePositiveNumber(market.price_usd, `${path}.market.price_usd`);
  const marketCapUsd = nullablePositiveNumber(
    market.market_cap_usd,
    `${path}.market.market_cap_usd`,
  );
  const priceObservedAtMs = nullableNonnegativeInteger(
    market.price_observed_at_ms,
    `${path}.market.price_observed_at_ms`,
  );
  const marketCapObservedAtMs = nullableNonnegativeInteger(
    market.market_cap_observed_at_ms,
    `${path}.market.market_cap_observed_at_ms`,
  );
  if ((priceUsd === null) !== (priceObservedAtMs === null)) {
    fail(`${path}.market.price_observed_at_ms`);
  }
  if ((marketCapUsd === null) !== (marketCapObservedAtMs === null)) {
    fail(`${path}.market.market_cap_observed_at_ms`);
  }
  if (priceChange !== null && (priceUsd === null || priceObservedAtMs === null)) {
    fail(`${path}.market.price_change_since_signal`);
  }
  const triggerSourceEventAtMs = nonnegativeInteger(
    item.trigger_source_event_at_ms,
    `${path}.trigger_source_event_at_ms`,
  );
  const qualifiedAtMs = nonnegativeInteger(item.qualified_at_ms, `${path}.qualified_at_ms`);
  if (qualifiedAtMs < triggerSourceEventAtMs) fail(`${path}.qualified_at_ms`);
  if (
    priceChange !== null &&
    priceObservedAtMs !== null &&
    priceObservedAtMs < triggerSourceEventAtMs
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
    trigger_source_event_at_ms: triggerSourceEventAtMs,
    qualified_at_ms: qualifiedAtMs,
    why_now: {
      current_mentions: currentMentions,
      prior_mentions: priorMentions,
      mention_delta: mentionDelta,
    },
    evidence: {
      independent_author_count: nonnegativeInteger(
        evidence.independent_author_count,
        `${path}.evidence.independent_author_count`,
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
      price_usd: priceUsd,
      price_observed_at_ms: priceObservedAtMs,
      price_change_since_signal: priceChange,
      market_cap_usd: marketCapUsd,
      market_cap_observed_at_ms: marketCapObservedAtMs,
    },
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

function tokenRadarStaleReason(
  value: unknown,
  path: string,
): "source_unavailable" | "projection_failed" | null {
  if (value === null || value === "source_unavailable" || value === "projection_failed") {
    return value;
  }
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
