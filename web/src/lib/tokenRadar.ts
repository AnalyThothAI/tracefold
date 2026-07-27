import type {
  AssetFlowData,
  AssetFlowRow,
  Decision,
  FactorPoint,
  MarketContext,
  MarketObservationSnapshot,
  ScoreBlock,
  ScoreContribution,
  TimingBlock,
  TokenFactorFamily,
  TokenFactorSnapshot,
  TokenFlowItem,
  TradeabilityBlock,
} from "@lib/types";

import { TOKEN_FACTOR_SNAPSHOT_SCHEMA, requireTokenFactorSnapshot } from "./tokenFactorSnapshot";

export function sortTokenItems(items: TokenFlowItem[]): TokenFlowItem[] {
  const copy = [...items];
  return copy.sort((a, b) => b.opportunity.score - a.opportunity.score);
}

export function countDecisions(items: TokenFlowItem[]): Record<Decision, number> {
  return items.reduce<Record<Decision, number>>(
    (counts, item) => {
      counts[item.opportunity.decision] += 1;
      return counts;
    },
    { driver: 0, watch: 0, investigate: 0, discard: 0 },
  );
}

export function assetFlowRows(data?: AssetFlowData | null): AssetFlowRow[] {
  if (!data) {
    return [];
  }
  return [...data.targets, ...data.attention];
}

export function tokenRadarItems(
  data: AssetFlowData | null | undefined,
  window: TokenFlowItem["flow"]["window"],
): TokenFlowItem[] {
  if (!data) {
    return [];
  }
  return assetFlowRows(data).map((row) => tokenRadarRowToTokenItem(row, window));
}

export function tokenRadarRowToTokenItem(
  row: AssetFlowRow,
  window: TokenFlowItem["flow"]["window"],
): TokenFlowItem {
  const snapshot = requiredFactorSnapshot(row);
  const subject = requiredObject(snapshot.subject, "factor_snapshot.subject") as Record<
    string,
    unknown
  >;
  const gates = requiredObject(
    snapshot.gates,
    "factor_snapshot.gates",
  ) as TokenFactorSnapshot["gates"];
  const dataHealth = requiredObject(
    snapshot.data_health,
    "factor_snapshot.data_health",
  ) as TokenFactorSnapshot["data_health"];
  const normalization = requiredObject(
    snapshot.normalization,
    "factor_snapshot.normalization",
  ) as TokenFactorSnapshot["normalization"];
  const attentionFamily = requiredFamily(snapshot, "social_heat");
  const diffusionFamily = requiredFamily(snapshot, "social_propagation");
  const timingFamily = requiredFamily(snapshot, "timing_risk");
  const attention = familyFacts(attentionFamily);
  const diffusionFacts = familyFacts(diffusionFamily);
  const timingFacts = familyFacts(timingFamily);
  const snapshotMarket = requiredObject(snapshot.market, "factor_snapshot.market") as Record<
    string,
    unknown
  >;
  const marketContext = requiredMarketContext(snapshot.market);
  const eventAnchor = marketContext.event_anchor;
  const decisionLatest = marketContext.decision_latest;
  const readiness = marketContext.readiness;
  const anchorPriceValue =
    optionalNullableNumber(eventAnchor?.price_usd) ??
    optionalNullableNumber(eventAnchor?.price_quote);
  const livePriceValue =
    optionalNullableNumber(decisionLatest?.price_usd) ??
    optionalNullableNumber(decisionLatest?.price_quote);
  const displayPrice = livePriceValue ?? anchorPriceValue;
  const liveMarketHasPrice = livePriceValue !== null;
  const marketStatus = marketDisplayStatus(readiness, liveMarketHasPrice);
  const priceChangeSinceSocialPct = liveDeltaFromAnchor(livePriceValue, anchorPriceValue);
  const priceChangeBeforeSocialPct = null;
  const priceChangeStatus = priceChangeStatusFromMarketContext(
    readiness,
    priceChangeSinceSocialPct,
  );
  const composite = requiredObject(snapshot.composite, "factor_snapshot.composite") as Record<
    string,
    unknown
  >;
  const familyScores = recordValue(composite.family_scores);
  const provenance = recordValue(snapshot.provenance);
  const radar = recordValue(row.radar);
  const radarMeta = {
    lane: optionalString(radar.lane),
    rank: optionalNullableNumber(radar.rank),
    listed_at_ms: optionalNullableNumber(radar.listed_at_ms),
    computed_at_ms:
      optionalNullableNumber(radar.computed_at_ms) ??
      optionalNullableNumber(provenance.computed_at_ms),
    source_max_received_at_ms: optionalNullableNumber(radar.source_max_received_at_ms),
  };
  const hasRadarMeta = Object.values(radarMeta).some((value) => value !== null);
  const mentions5m = requiredNumber(
    attention.mentions_5m,
    "factor_snapshot.social_heat.mentions_5m",
  );
  const mentions1h = requiredNumber(
    attention.mentions_1h,
    "factor_snapshot.social_heat.mentions_1h",
  );
  const mentions4h = requiredNumber(
    attention.mentions_4h,
    "factor_snapshot.social_heat.mentions_4h",
  );
  const mentions24h = requiredNumber(
    attention.mentions_24h,
    "factor_snapshot.social_heat.mentions_24h",
  );
  const mentions = requiredNumber(
    attention[`mentions_${window}`],
    `factor_snapshot.social_heat.mentions_${window}`,
  );
  const authors = requiredNumber(
    attention.unique_authors,
    "factor_snapshot.social_heat.unique_authors",
  );
  const latestSeenMs = requiredNumber(
    attention.latest_seen_ms,
    "factor_snapshot.social_heat.latest_seen_ms",
  );
  const previousMentions = optionalNumber(attention.previous_mentions) ?? 0;
  const mentionDelta = optionalNumber(attention.mention_delta) ?? mentions - previousMentions;
  const mentionDeltaPct = optionalNullableNumber(attention.mention_delta_pct);
  const zScore = optionalNullableNumber(attention.z_score);
  const newBurstScore = optionalNullableNumber(attention.new_burst_score);
  const streamShare = optionalNumber(attention.stream_share) ?? 0;
  const baselineStatus =
    optionalString(attention.baseline_status) ?? String(attentionFamily.data_health ?? "snapshot");
  const baselineSampleCount = optionalNumber(attention.baseline_sample_count) ?? 0;
  const sourceEventIds = requiredStringArray(
    provenance.source_event_ids,
    "factor_snapshot.provenance.source_event_ids",
  );
  const isChainAsset = subject.target_type === "Asset";
  const isCexToken = subject.target_type === "CexToken";
  const displaySymbol = stringValue(subject.symbol);
  const targetId = requiredString(subject.target_id, "factor_snapshot.subject.target_id");
  const address = isChainAsset ? stringValue(subject.address) : null;
  const cexPricefeedId = isCexToken ? stringValue(subject.pricefeed_id) : null;
  const parsedCexPricefeed = parseCexPricefeedId(cexPricefeedId);
  const nativeMarketId = isCexToken ? (parsedCexPricefeed?.instId ?? null) : null;
  const identityKey = targetId;
  const resolved = Boolean(targetId && subject.target_type);
  const resolutionReasons = row.resolution.reason_codes;
  const candidateCount = row.resolution.candidate_ids.length;
  const discoveryStatus = discoveryStatusSummary(row.resolution.discovery);
  const marketObservationStatus = marketStatus;
  const marketHasUsableSnapshot = liveMarketHasPrice || readiness.anchor_status === "ready";
  const heat = scoreBlockFromFamily(attentionFamily, "social_heat", "social_heat");
  const propagation = scoreBlockFromFamily(diffusionFamily, "social_propagation", "propagation");
  const quality = scoreBlockFromFamily(diffusionFamily, "social_propagation", "discussion_quality");
  const anchoredMarketCap = optionalNullableNumber(eventAnchor?.market_cap_usd);
  const anchoredLiquidity = optionalNullableNumber(eventAnchor?.liquidity_usd);
  const anchoredHolders = optionalNullableNumber(eventAnchor?.holders);
  const anchoredVolume24h = optionalNullableNumber(eventAnchor?.volume_24h_usd);
  const displayMarketCap = isCexToken
    ? null
    : (optionalNullableNumber(decisionLatest?.market_cap_usd) ?? anchoredMarketCap);
  const displayLiquidity =
    optionalNullableNumber(decisionLatest?.liquidity_usd) ?? anchoredLiquidity;
  const displayHolders = optionalNullableNumber(decisionLatest?.holders) ?? anchoredHolders;
  const displayVolume24h =
    optionalNullableNumber(decisionLatest?.volume_24h_usd) ?? anchoredVolume24h;
  const marketFactsForTradeability = {
    ...(decisionLatest ?? {}),
    market_cap_usd: displayMarketCap,
    liquidity_usd: displayLiquidity,
  };
  const tradeability = tradeabilityFromGatesAndHealth(
    gates,
    dataHealth,
    marketFactsForTradeability,
  );
  const timing = scoreBlockFromFamily(timingFamily, "timing_risk", "timing");
  const recommendedDecision = requiredString(
    optionalString(composite.recommended_decision),
    "factor_snapshot.composite.recommended_decision",
  );
  const decision = decisionFromRecommendation(recommendedDecision);
  const heatStatus =
    optionalString(attention.status) ??
    heatStatusFromScore(heat.score, attentionFamily.data_health);
  const timingStatus = timingStatusFromSnapshot(marketStatus, timing.risks, resolved);
  const chaseRisk =
    timing.risks.includes("chase_risk") || timing.risks.includes("timing_chase_risk");
  const marketPrice = displayPrice;
  const marketProvider = firstString(decisionLatest?.provider, eventAnchor?.provider);
  const chain = isChainAsset ? stringValue(subject.chain) : null;
  const blockedReasons = requiredStringArray(
    gates.blocked_reasons,
    "factor_snapshot.gates.blocked_reasons",
  );
  const opportunityScore = requiredNumber(
    composite.rank_score,
    "factor_snapshot.composite.rank_score",
  );
  const opportunity = {
    ...scoreBlockFromComposite(snapshot, blockedReasons),
    decision,
    decision_priority: decision === "driver" ? 3 : decision === "watch" ? 2 : 1,
    hard_risks: blockedReasons,
    components: {
      heat: scoreFromFamilyScores(familyScores, "social_heat"),
      propagation: scoreFromFamilyScores(familyScores, "social_propagation"),
      timing: scoreFromFamilyScores(familyScores, "timing_risk"),
    },
    score: opportunityScore,
  };
  return {
    identity: {
      identity_key: identityKey,
      identity_status: row.resolution.status,
      target_type: stringValue(subject.target_type),
      target_id: targetId,
      asset_id: isChainAsset ? (targetId ?? undefined) : undefined,
      asset_type: stringValue(subject.target_type),
      venue_type: isCexToken ? "cex" : isChainAsset ? "dex" : null,
      exchange: isCexToken
        ? (parsedCexPricefeed?.exchange ?? normalizeCexExchange(marketProvider))
        : null,
      inst_id: isCexToken ? nativeMarketId : null,
      inst_type: isCexToken ? (parsedCexPricefeed?.instType ?? null) : null,
      chain,
      address,
      symbol: displaySymbol,
      resolution_reasons: resolutionReasons,
      lookup_keys: row.resolution.lookup_keys,
      candidate_count: candidateCount,
      discovery_status: discoveryStatus,
    },
    market: {
      event_anchor: eventAnchor,
      decision_latest: decisionLatest,
      readiness,
      market_status: marketStatus,
      price: marketPrice,
      price_status: liveMarketHasPrice ? readiness.latest_status : readiness.anchor_status,
      market_cap: displayMarketCap,
      market_cap_status: fieldStatusFromNullableValue(displayMarketCap),
      liquidity: displayLiquidity,
      liquidity_status: fieldStatusFromNullableValue(displayLiquidity),
      pool_status: marketHasUsableSnapshot ? "ready" : "missing",
      holder_count: displayHolders,
      holder_count_status: fieldStatusFromNullableValue(displayHolders),
      volume_24h: displayVolume24h,
      volume_24h_status: fieldStatusFromNullableValue(displayVolume24h),
      provider: marketProvider,
      snapshot_age_ms: snapshotAgeMs(decisionLatest, latestSeenMs),
      snapshot_received_at_ms:
        optionalNullableNumber(decisionLatest?.observed_at_ms) ??
        optionalNullableNumber(eventAnchor?.observed_at_ms),
      social_signal_start_ms:
        optionalNullableNumber(eventAnchor?.received_at_ms) ??
        optionalNumber(timingFacts.social_signal_start_ms) ??
        latestSeenMs,
      reference_ms: latestSeenMs,
      price_at_social_start: anchorPriceValue,
      price_at_reference: livePriceValue,
      price_change_since_social_pct: priceChangeSinceSocialPct,
      price_before_social_start: null,
      price_change_before_social_pct: priceChangeBeforeSocialPct,
      price_at_first_snapshot:
        optionalNullableNumber(snapshotMarket.price_at_first_snapshot) ?? anchorPriceValue,
      first_snapshot_observed_at_ms:
        optionalNullableNumber(snapshotMarket.first_snapshot_observed_at_ms) ??
        optionalNullableNumber(eventAnchor?.observed_at_ms),
      price_change_since_first_snapshot_pct: null,
      market_observation_status: marketObservationStatus,
      price_change_status: priceChangeStatus,
    },
    flow: {
      window,
      window_start_ms: null,
      window_end_ms: latestSeenMs,
      mentions,
      direct_mentions: resolved ? mentions : 0,
      symbol_mentions: mentions,
      weighted_mentions: mentions,
      avg_attribution_confidence: undefined,
      previous_mentions: previousMentions,
      mention_delta: mentionDelta,
      mention_delta_pct: mentionDeltaPct,
      z_score: zScore,
      new_burst_score: newBurstScore,
      stream_dominance: 0,
      baseline_status: baselineStatus,
      baseline_sample_count: baselineSampleCount,
    },
    social_heat: {
      ...heat,
      window,
      mentions,
      mentions_5m: mentions5m,
      mentions_1h: mentions1h,
      mentions_4h: mentions4h,
      mentions_24h: mentions24h,
      weighted_mentions: mentions,
      previous_mentions: previousMentions,
      mention_delta: mentionDelta,
      mention_delta_pct: mentionDeltaPct,
      z_score: zScore,
      new_burst_score: newBurstScore,
      stream_share: streamShare,
      status: heatStatus,
    },
    discussion_quality: {
      ...quality,
      evidence_specificity: 0,
      avg_post_quality: quality.score,
      avg_attribution_confidence: 0,
      duplicate_text_share: optionalNumber(diffusionFacts.duplicate_text_share) ?? 0,
      informative_post_count:
        optionalNumber(diffusionFacts.informative_post_count) ??
        Math.min(mentions, authors || mentions),
    },
    propagation: {
      ...propagation,
      independent_authors: optionalNumber(diffusionFacts.independent_authors) ?? authors,
      effective_authors: optionalNumber(diffusionFacts.effective_authors) ?? authors,
      new_authors: optionalNumber(diffusionFacts.independent_authors) ?? authors,
      top_author_share:
        optionalNumber(diffusionFacts.top_author_share) ?? (authors ? 1 / authors : 0),
      duplicate_text_share: optionalNumber(diffusionFacts.duplicate_text_share) ?? 0,
      author_entropy: authors > 1 ? 1 : 0,
      reproduction_rate: null,
      phase: authors >= 3 ? "expansion" : authors >= 2 ? "ignition" : "seed",
      top_authors: [],
    },
    tradeability: {
      ...tradeability,
      identity_tradeable: Boolean(tradeability.identity_tradeable),
      market_fresh: Boolean(tradeability.market_fresh),
      market_cap_present: Boolean(tradeability.market_cap_present),
      liquidity_present: Boolean(tradeability.liquidity_present),
      pool_present: Boolean(tradeability.pool_present),
      hard_risks: tradeability.hard_risks ?? tradeability.risks,
    },
    timing: {
      score: timing.score,
      score_version: timing.score_version,
      status: timingStatus,
      social_signal_start_ms:
        optionalNullableNumber(eventAnchor?.received_at_ms) ??
        optionalNumber(timingFacts.social_signal_start_ms) ??
        latestSeenMs,
      price_change_since_social_pct: priceChangeSinceSocialPct,
      price_change_before_social_pct: priceChangeBeforeSocialPct,
      market_observation_status: marketObservationStatus,
      chase_risk: chaseRisk,
      reasons: timing.reasons,
      risks: timing.risks,
      contributions: timing.contributions,
      risk_caps: timing.risk_caps,
    },
    opportunity,
    profile: row.profile ?? null,
    factor_data_health: dataHealth,
    factor_gates: gates,
    factor_normalization: normalization,
    radar: hasRadarMeta ? radarMeta : undefined,
    evidence_total_count: sourceEventIds.length,
    posts_query: {
      target_type: stringValue(subject.target_type),
      target_id: targetId,
      window,
      range: "current_window",
    },
    timeline_query: {
      target_type: stringValue(subject.target_type),
      target_id: targetId,
      window,
    },
  };
}

type FactorFamily = TokenFactorFamily;
type ScoreBlockWithHardRisks = ScoreBlock & { hard_risks: string[] };

function requiredFactorSnapshot(row: AssetFlowRow): TokenFactorSnapshot {
  try {
    return requireTokenFactorSnapshot(row.factor_snapshot, "factor_snapshot");
  } catch (error) {
    if (error instanceof Error) {
      throw new Error(
        error.message.replace("token_factor_snapshot_contract:", "token_radar_contract:"),
      );
    }
    throw error;
  }
}

function requiredMarketContext(value: unknown): MarketContext {
  if (!value || typeof value !== "object") {
    throw new Error("token_radar_contract:market");
  }
  const market = value as Partial<MarketContext>;
  if (
    market.event_anchor !== null &&
    market.event_anchor !== undefined &&
    typeof market.event_anchor !== "object"
  ) {
    throw new Error("token_radar_contract:market.event_anchor");
  }
  if (
    market.decision_latest !== null &&
    market.decision_latest !== undefined &&
    typeof market.decision_latest !== "object"
  ) {
    throw new Error("token_radar_contract:market.decision_latest");
  }
  if (!market.readiness || typeof market.readiness !== "object") {
    throw new Error("token_radar_contract:market.readiness");
  }
  for (const key of ["anchor_status", "latest_status", "dex_floor_status"] as const) {
    if (typeof market.readiness[key] !== "string" || !market.readiness[key]) {
      throw new Error(`token_radar_contract:market.readiness.${key}`);
    }
  }
  for (const key of ["missing_fields", "stale_fields"] as const) {
    if (!Array.isArray(market.readiness[key])) {
      throw new Error(`token_radar_contract:market.readiness.${key}`);
    }
  }
  return {
    event_anchor: (market.event_anchor ?? null) as MarketObservationSnapshot | null,
    decision_latest: (market.decision_latest ?? null) as MarketObservationSnapshot | null,
    readiness: market.readiness as MarketContext["readiness"],
  };
}

function requiredFamily(
  snapshot: TokenFactorSnapshot,
  familyName: keyof TokenFactorSnapshot["families"],
): FactorFamily {
  const family = snapshot.families?.[familyName];
  if (!family || typeof family !== "object") {
    throw new Error(`token_radar_contract:factor_snapshot.families.${familyName}`);
  }
  return family;
}

function familyFacts(family: FactorFamily): Record<string, unknown> {
  return recordValue(family.facts);
}

function marketDisplayStatus(
  readiness: MarketContext["readiness"],
  liveMarketHasPrice: boolean,
): string {
  if (liveMarketHasPrice) {
    return readiness.latest_status === "missing" ? "live" : readiness.latest_status;
  }
  return readiness.anchor_status === "ready" ? "anchored" : "missing";
}

function scoreBlockFromFamily(
  family: FactorFamily,
  factorFamily: string,
  scoreVersionFamily: string,
): ScoreBlockWithHardRisks {
  const score = Math.round(requiredNumber(family.score, `factor_snapshot.${factorFamily}.score`));
  const points = factorPoints(family);
  const risks = uniqueStrings(
    points
      .flatMap((point) => stringArray(point.risk_flags))
      .filter((item): item is string => Boolean(item)),
  );
  return {
    score,
    score_version: `${TOKEN_FACTOR_SNAPSHOT_SCHEMA}:${scoreVersionFamily}`,
    reasons: reasonsFromFamily(factorFamily, points, risks),
    risks,
    hard_risks: [],
    contributions: contributionsFromFactors(points, factorFamily, score),
    risk_caps: [],
  };
}

function reasonsFromFamily(factorFamily: string, points: FactorPoint[], risks: string[]): string[] {
  if (factorFamily === "timing_risk") {
    return risks.length ? risks : [];
  }
  return uniqueStrings(points.map((point) => `${point.family}.${point.key}`));
}

function scoreBlockFromComposite(
  snapshot: TokenFactorSnapshot,
  blockedReasons: string[],
): ScoreBlockWithHardRisks {
  const composite = requiredObject(snapshot.composite, "factor_snapshot.composite") as Record<
    string,
    unknown
  >;
  const score = Math.round(
    requiredNumber(composite.rank_score, "factor_snapshot.composite.rank_score"),
  );
  return {
    score,
    score_version: `${TOKEN_FACTOR_SNAPSHOT_SCHEMA}:composite`,
    reasons: [optionalString(composite.recommended_decision) ?? "factor_snapshot_composite"],
    risks: blockedReasons,
    hard_risks: blockedReasons,
    contributions: Object.entries(recordValue(composite.family_scores)).map(([feature, value]) => ({
      feature,
      value: optionalNumber(value) ?? 0,
      reason: "factor_family_score",
    })),
    risk_caps: blockedReasons.map((risk) => ({ risk, cap: 39 })),
  };
}

function tradeabilityFromGatesAndHealth(
  gates: TokenFactorSnapshot["gates"],
  dataHealth: TokenFactorSnapshot["data_health"],
  marketFacts: MarketObservationSnapshot,
): TradeabilityBlock {
  const blockedReasons = requiredStringArray(
    gates.blocked_reasons,
    "factor_snapshot.gates.blocked_reasons",
  );
  const riskReasons = stringArray(gates.risk_reasons);
  const marketReady = dataHealth.market === "ready";
  const marketCapPresent = optionalNullableNumber(marketFacts.market_cap_usd) !== null;
  const liquidityPresent = optionalNullableNumber(marketFacts.liquidity_usd) !== null;
  const score = gates.eligible_for_high_alert ? 100 : marketReady ? 60 : 0;
  return {
    score,
    score_version: `${TOKEN_FACTOR_SNAPSHOT_SCHEMA}:gates`,
    reasons: [marketReady ? "market_health_ready" : "market_health_not_ready"],
    risks: uniqueStrings([...blockedReasons, ...riskReasons]),
    hard_risks: blockedReasons,
    contributions: [
      {
        feature: "gates.eligible_for_high_alert",
        value: gates.eligible_for_high_alert ? 100 : 0,
        reason: "factor_snapshot.gates",
      },
      {
        feature: "data_health.market",
        value: marketReady ? 100 : 0,
        reason: String(dataHealth.market),
      },
    ],
    risk_caps: blockedReasons.map((risk) => ({ risk, cap: 39 })),
    identity_tradeable: dataHealth.identity === "ready",
    market_fresh: marketReady,
    market_cap_present: marketCapPresent,
    liquidity_present: liquidityPresent,
    pool_present: marketReady,
  };
}

function factorPoints(family: FactorFamily): FactorPoint[] {
  const factors = recordValue(family.factors);
  return Object.values(factors).filter((value): value is FactorPoint =>
    Boolean(value && typeof value === "object"),
  );
}

function contributionsFromFactors(
  points: FactorPoint[],
  fallbackFeature: string,
  fallbackScore: number,
): ScoreContribution[] {
  const contributions = points.map((point) => ({
    feature: `${point.family}.${point.key}`,
    value: optionalNumber(point.score) ?? 0,
    reason: optionalString(point.data_health) ?? "factor_snapshot",
  }));
  return contributions.length
    ? contributions
    : [{ feature: fallbackFeature, value: fallbackScore, reason: "factor_snapshot" }];
}

function scoreFromFamilyScores(familyScores: Record<string, unknown>, key: string): number {
  return requiredNumber(familyScores[key], `factor_snapshot.composite.family_scores.${key}`);
}

function decisionFromRecommendation(value: string | null | undefined): Decision {
  if (value === "high_alert") return "driver";
  if (value === "watch") return "watch";
  if (value === "discard") return "discard";
  throw new Error("token_radar_contract:factor_snapshot.composite.recommended_decision");
}

function heatStatusFromScore(score: number, dataHealth: unknown): string {
  if (dataHealth === "missing" || dataHealth === "partial") return String(dataHealth);
  if (score >= 80) return "burst";
  if (score >= 50) return "rising";
  return "cold";
}

function timingStatusFromSnapshot(
  marketStatus: string,
  risks: string[],
  resolved: boolean,
): TimingBlock["status"] {
  if (risks.includes("chase_risk") || risks.includes("timing_chase_risk")) return "chase_risk";
  if (marketStatus === "missing") return resolved ? "market_pending" : "market_unavailable";
  return "neutral";
}

function liveDeltaFromAnchor(livePrice: number | null, anchorPrice: number | null): number | null {
  if (livePrice === null || anchorPrice === null || anchorPrice === 0) {
    return null;
  }
  return (livePrice - anchorPrice) / Math.abs(anchorPrice);
}

function priceChangeStatusFromMarketContext(
  readiness: MarketContext["readiness"],
  priceChangeSinceSocialPct: number | null,
): string {
  if (readiness.anchor_status !== "ready") return "missing_anchor";
  if (priceChangeSinceSocialPct !== null) return "ready";
  if (readiness.latest_status === "missing") return "missing_latest";
  return "missing_live_price";
}

function snapshotAgeMs(
  observation: MarketObservationSnapshot | null,
  referenceMs: number,
): number | null {
  const observedAtMs = optionalNullableNumber(observation?.observed_at_ms);
  if (observedAtMs === null) {
    return null;
  }
  return Math.max(0, referenceMs - observedAtMs);
}

function requiredNumber(value: unknown, field: string): number {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    throw new Error(`token_radar_contract:${field}`);
  }
  return value;
}

function requiredString(value: unknown, field: string): string {
  if (typeof value !== "string" || !value) {
    throw new Error(`token_radar_contract:${field}`);
  }
  return value;
}

function requiredObject<T extends object>(value: T | null | undefined, field: string): T {
  if (!value || typeof value !== "object") {
    throw new Error(`token_radar_contract:${field}`);
  }
  return value;
}

function requiredArray<T>(value: unknown, field: string): T[] {
  if (!Array.isArray(value)) {
    throw new Error(`token_radar_contract:${field}`);
  }
  return value as T[];
}

function requiredStringArray(value: unknown, field: string): string[] {
  return requiredArray(value, field).map((item) => requiredString(item, field));
}

function optionalNumber(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

function optionalNullableNumber(value: unknown): number | null {
  if (value === null || value === undefined) {
    return null;
  }
  return optionalNumber(value) ?? null;
}

function optionalString(value: unknown): string | null {
  return typeof value === "string" && value.length ? value : null;
}

function firstString(...values: unknown[]): string | null {
  for (const value of values) {
    const text = optionalString(value);
    if (text) {
      return text;
    }
  }
  return null;
}

function parseCexPricefeedId(
  pricefeedId?: string | null,
): { exchange: string; instType: string; instId: string } | null {
  const parts = pricefeedId?.trim().split(":") ?? [];
  if (parts.length < 5 || parts[0] !== "pricefeed" || parts[1] !== "cex") {
    return null;
  }
  return {
    exchange: normalizeCexExchange(parts[2]) ?? parts[2].toLowerCase(),
    instType: normalizeCexInstType(parts[3]) ?? parts[3].toUpperCase(),
    instId: parts.slice(4).join(":"),
  };
}

function normalizeCexExchange(value: unknown): string | null {
  const text = optionalString(value)?.trim().toLowerCase();
  if (!text) {
    return null;
  }
  if (text === "okx" || text === "okx_cex") {
    return "okx";
  }
  if (text === "binance" || text === "binance_cex") {
    return "binance";
  }
  return text;
}

function normalizeCexInstType(value: unknown): string | null {
  const text = optionalString(value)?.trim().toUpperCase();
  if (!text) {
    return null;
  }
  if (text === "CEX_SPOT") {
    return "SPOT";
  }
  if (text === "CEX_SWAP" || text === "PERP" || text === "PERPETUAL") {
    return "SWAP";
  }
  return text;
}

function stringValue(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}

function fieldStatusFromNullableValue(value: unknown): string {
  return optionalNullableNumber(value) === null ? "missing" : "live";
}

function stringArray(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string" && Boolean(item))
    : [];
}

function uniqueStrings(items: string[]): string[] {
  return Array.from(new Set(items));
}

function recordValue(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function discoveryStatusSummary(discovery: AssetFlowRow["resolution"]["discovery"]): string | null {
  if (!discovery?.length) {
    return null;
  }
  const statuses = Array.from(new Set(discovery.map((item) => item.status).filter(Boolean)));
  if (statuses.length === 1) {
    const candidateTotal = discovery.reduce(
      (sum, item) => sum + Number(item.candidate_count ?? 0),
      0,
    );
    return candidateTotal > 0 ? `${statuses[0]}:${candidateTotal}` : String(statuses[0]);
  }
  return statuses.join("+");
}
