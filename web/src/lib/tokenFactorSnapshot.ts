import type { TokenFactorFamilyKey, TokenFactorSnapshot } from "@lib/types";

export const TOKEN_FACTOR_SNAPSHOT_SCHEMA = "token_factor_snapshot_v5_provider_neutral";
const TOP_LEVEL_KEYS = new Set([
  "schema_version",
  "subject",
  "market",
  "gates",
  "data_health",
  "families",
  "normalization",
  "composite",
  "provenance",
]);
const ALPHA_FAMILIES: TokenFactorFamilyKey[] = ["social_heat", "social_propagation", "timing_risk"];
const FACTOR_VALUE_KEYS = new Set<string>(ALPHA_FAMILIES);
const FAMILY_KEYS = new Set(["raw_score", "score", "weight", "data_health", "facts", "factors"]);
const PROVENANCE_KEYS = new Set(["source_event_ids", "computed_at_ms"]);
const GATES_KEYS = new Set([
  "eligible_for_high_alert",
  "max_decision",
  "blocked_reasons",
  "risk_reasons",
]);
const NORMALIZATION_KEYS = new Set([
  "status",
  "cohort_status",
  "cohort",
  "factor_ranks",
  "alpha_rank",
]);
const COMPOSITE_KEYS = new Set([
  "raw_alpha_score",
  "rank_score",
  "family_scores",
  "recommended_decision",
]);
const SUBJECT_KEYS = new Set([
  "target_type",
  "target_id",
  "symbol",
  "target_market_type",
  "chain",
  "address",
  "pricefeed_id",
]);
const TOKEN_RADAR_DECISIONS = new Set(["discard", "watch", "high_alert"]);
const MARKET_REQUIRED_KEYS = new Set(["event_anchor", "decision_latest", "readiness"]);
const MARKET_OPTIONAL_KEYS = new Set(["capture_method", "capture_reason", "tick_lag_ms"]);
const MARKET_KEYS = new Set([...MARKET_REQUIRED_KEYS, ...MARKET_OPTIONAL_KEYS]);
const MARKET_READINESS_KEYS = new Set([
  "anchor_status",
  "latest_status",
  "dex_floor_status",
  "missing_fields",
  "stale_fields",
]);
export function requireTokenFactorSnapshot(
  value: unknown,
  fieldName = "factor_snapshot",
): TokenFactorSnapshot {
  if (!isRecord(value)) {
    throw new Error(`token_factor_snapshot_contract:${fieldName}`);
  }
  if (value.schema_version !== TOKEN_FACTOR_SNAPSHOT_SCHEMA) {
    throw new Error(`token_factor_snapshot_contract:${fieldName}.schema_version`);
  }
  const keys = Object.keys(value);
  const missing = [...TOP_LEVEL_KEYS].find((key) => !keys.includes(key));
  if (missing) {
    throw new Error(`token_factor_snapshot_contract:${fieldName}.${missing}`);
  }
  const extra = keys.find((key) => !TOP_LEVEL_KEYS.has(key));
  if (extra) {
    throw new Error(`token_factor_snapshot_contract:${fieldName}.${extra}`);
  }

  for (const key of [
    "subject",
    "market",
    "gates",
    "data_health",
    "families",
    "normalization",
    "composite",
    "provenance",
  ] as const) {
    if (!isRecord(value[key])) {
      throw new Error(`token_factor_snapshot_contract:${fieldName}.${key}`);
    }
  }

  const subject = value.subject as Record<string, unknown>;
  requireExactKeys(subject, SUBJECT_KEYS, `${fieldName}.subject`);
  const gates = value.gates as Record<string, unknown>;
  requireExactKeys(gates, GATES_KEYS, `${fieldName}.gates`);
  if (typeof gates.eligible_for_high_alert !== "boolean") {
    throw new Error(`token_factor_snapshot_contract:${fieldName}.gates.eligible_for_high_alert`);
  }
  if (!TOKEN_RADAR_DECISIONS.has(String(gates.max_decision))) {
    throw new Error(`token_factor_snapshot_contract:${fieldName}.gates.max_decision`);
  }
  for (const key of ["blocked_reasons", "risk_reasons"] as const) {
    requireStringArray(gates[key], `${fieldName}.gates.${key}`, false);
  }

  const market = value.market as Record<string, unknown>;
  requireAllowedKeys(market, MARKET_REQUIRED_KEYS, MARKET_KEYS, `${fieldName}.market`);
  for (const key of ["event_anchor", "decision_latest"] as const) {
    if (market[key] !== null && !isRecord(market[key])) {
      throw new Error(`token_factor_snapshot_contract:${fieldName}.market.${key}`);
    }
  }
  for (const key of ["capture_method", "capture_reason"] as const) {
    if (market[key] !== undefined && market[key] !== null && typeof market[key] !== "string") {
      throw new Error(`token_factor_snapshot_contract:${fieldName}.market.${key}`);
    }
  }
  if (
    market.tick_lag_ms !== undefined &&
    market.tick_lag_ms !== null &&
    (typeof market.tick_lag_ms !== "number" || !Number.isFinite(market.tick_lag_ms))
  ) {
    throw new Error(`token_factor_snapshot_contract:${fieldName}.market.tick_lag_ms`);
  }
  if (!isRecord(market.readiness)) {
    throw new Error(`token_factor_snapshot_contract:${fieldName}.market.readiness`);
  }
  requireExactKeys(market.readiness, MARKET_READINESS_KEYS, `${fieldName}.market.readiness`);
  for (const key of ["missing_fields", "stale_fields"] as const) {
    if (!Array.isArray(market.readiness[key])) {
      throw new Error(`token_factor_snapshot_contract:${fieldName}.market.readiness.${key}`);
    }
  }

  const families = value.families as Record<string, unknown>;
  const familyKeys = Object.keys(families);
  const extraFamily = familyKeys.find(
    (family) => !ALPHA_FAMILIES.includes(family as TokenFactorFamilyKey),
  );
  if (extraFamily) {
    throw new Error(`token_factor_snapshot_contract:${fieldName}.families.${extraFamily}`);
  }
  const missingFamily = ALPHA_FAMILIES.find((family) => !familyKeys.includes(family));
  if (missingFamily) {
    throw new Error(`token_factor_snapshot_contract:${fieldName}.families.${missingFamily}`);
  }
  for (const family of ALPHA_FAMILIES) {
    const familyBlock = families[family];
    if (!isRecord(familyBlock)) {
      throw new Error(`token_factor_snapshot_contract:${fieldName}.families.${family}`);
    }
    requireExactKeys(familyBlock, FAMILY_KEYS, `${fieldName}.families.${family}`);
    for (const key of ["raw_score", "score", "weight"] as const) {
      if (typeof familyBlock[key] !== "number" || !Number.isFinite(familyBlock[key])) {
        throw new Error(`token_factor_snapshot_contract:${fieldName}.families.${family}.${key}`);
      }
    }
    if (typeof familyBlock.data_health !== "string" || !familyBlock.data_health) {
      throw new Error(`token_factor_snapshot_contract:${fieldName}.families.${family}.data_health`);
    }
    if (!isRecord(familyBlock.facts)) {
      throw new Error(`token_factor_snapshot_contract:${fieldName}.families.${family}.facts`);
    }
    if (!isRecord(familyBlock.factors)) {
      throw new Error(`token_factor_snapshot_contract:${fieldName}.families.${family}.factors`);
    }
  }

  const normalization = value.normalization as Record<string, unknown>;
  requireExactKeys(normalization, NORMALIZATION_KEYS, `${fieldName}.normalization`);
  for (const key of ["status", "cohort_status"] as const) {
    if (typeof normalization[key] !== "string" || !normalization[key]) {
      throw new Error(`token_factor_snapshot_contract:${fieldName}.normalization.${key}`);
    }
  }
  if (!isRecord(normalization.cohort)) {
    throw new Error(`token_factor_snapshot_contract:${fieldName}.normalization.cohort`);
  }
  if (!isRecord(normalization.factor_ranks)) {
    throw new Error(`token_factor_snapshot_contract:${fieldName}.normalization.factor_ranks`);
  }
  requireExactKeys(
    normalization.factor_ranks,
    FACTOR_VALUE_KEYS,
    `${fieldName}.normalization.factor_ranks`,
  );
  for (const family of ALPHA_FAMILIES) {
    const rank = normalization.factor_ranks[family];
    if (rank !== null && !isFiniteNumber(rank)) {
      throw new Error(
        `token_factor_snapshot_contract:${fieldName}.normalization.factor_ranks.${family}`,
      );
    }
  }
  if (normalization.alpha_rank !== null && !isFiniteNumber(normalization.alpha_rank)) {
    throw new Error(`token_factor_snapshot_contract:${fieldName}.normalization.alpha_rank`);
  }

  const composite = value.composite as Record<string, unknown>;
  requireExactKeys(composite, COMPOSITE_KEYS, `${fieldName}.composite`);
  for (const key of ["raw_alpha_score", "rank_score"] as const) {
    if (!isFiniteNumber(composite[key])) {
      throw new Error(`token_factor_snapshot_contract:${fieldName}.composite.${key}`);
    }
  }
  if (!isRecord(composite.family_scores)) {
    throw new Error(`token_factor_snapshot_contract:${fieldName}.composite.family_scores`);
  }
  requireExactKeys(
    composite.family_scores,
    FACTOR_VALUE_KEYS,
    `${fieldName}.composite.family_scores`,
  );
  for (const family of ALPHA_FAMILIES) {
    if (!isFiniteNumber(composite.family_scores[family])) {
      throw new Error(
        `token_factor_snapshot_contract:${fieldName}.composite.family_scores.${family}`,
      );
    }
  }
  if (!TOKEN_RADAR_DECISIONS.has(String(composite.recommended_decision))) {
    throw new Error(`token_factor_snapshot_contract:${fieldName}.composite.recommended_decision`);
  }
  const provenance = value.provenance as Record<string, unknown>;
  requireExactKeys(provenance, PROVENANCE_KEYS, `${fieldName}.provenance`);
  requireStringArray(provenance.source_event_ids, `${fieldName}.provenance.source_event_ids`, true);
  if (
    typeof provenance.computed_at_ms !== "number" ||
    !Number.isFinite(provenance.computed_at_ms)
  ) {
    throw new Error(`token_factor_snapshot_contract:${fieldName}.provenance.computed_at_ms`);
  }

  return value as TokenFactorSnapshot;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function isFiniteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

function requireStringArray(value: unknown, fieldName: string, requireNonEmpty: boolean): void {
  if (
    !Array.isArray(value) ||
    (requireNonEmpty && value.length === 0) ||
    value.some((item) => typeof item !== "string" || !item)
  ) {
    throw new Error(`token_factor_snapshot_contract:${fieldName}`);
  }
}

function requireExactKeys(
  value: Record<string, unknown>,
  allowed: Set<string>,
  fieldName: string,
): void {
  const keys = Object.keys(value);
  const missing = [...allowed].find((key) => !keys.includes(key));
  if (missing) {
    throw new Error(`token_factor_snapshot_contract:${fieldName}.${missing}`);
  }
  const extra = keys.find((key) => !allowed.has(key));
  if (extra) {
    throw new Error(`token_factor_snapshot_contract:${fieldName}.${extra}`);
  }
}

function requireAllowedKeys(
  value: Record<string, unknown>,
  required: Set<string>,
  allowed: Set<string>,
  fieldName: string,
): void {
  const keys = Object.keys(value);
  const missing = [...required].find((key) => !keys.includes(key));
  if (missing) {
    throw new Error(`token_factor_snapshot_contract:${fieldName}.${missing}`);
  }
  const extra = keys.find((key) => !allowed.has(key));
  if (extra) {
    throw new Error(`token_factor_snapshot_contract:${fieldName}.${extra}`);
  }
}
