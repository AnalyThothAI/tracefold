import { compactNumber, formatTokenPriceUsd, formatUsdCompact, shortAddress } from "@lib/format";
import type { TokenCaseDossier, TokenCasePostsData, TokenPostItem } from "@lib/types";
import type {
  TokenCaseMarketView,
  TokenCasePostEvent,
  TokenCaseTone,
  TokenCaseViewModel,
} from "@shared/model/tokenCaseViewModel";
import {
  buildTokenPostEventMarket,
  cleanText,
  numberValue,
  relativeTimeLabel,
  tokenPricePill,
} from "@shared/model/tokenPostEvent";

import type { TokenCaseRouteState } from "../state/tokenCaseRouteState";

export type BuildTokenCaseViewModelArgs = {
  dossier: TokenCaseDossier;
  route: TokenCaseRouteState;
  posts?: TokenCasePostsData | null;
  isLoadingPosts?: boolean;
  isFetchingNextPage?: boolean;
};

export function buildTokenCaseViewModel({
  dossier,
  route,
  posts,
  isLoadingPosts = false,
  isFetchingNextPage = false,
}: BuildTokenCaseViewModelArgs): TokenCaseViewModel {
  const target = dossier.target;
  const profileIdentity = dossier.profile?.identity;
  const symbol = cleanText(profileIdentity?.symbol) ?? cleanText(target.symbol);
  const name = cleanText(profileIdentity?.name);
  const title = symbol
    ? `$${symbol}${name && name !== symbol ? ` · ${name}` : ""}`
    : shortId(target.target_id);
  const mergedPosts = posts ?? dossier.posts;
  const market = buildMarketView(dossier);
  const livePrice = numberValue(dossier.market_live.price_usd);
  const timelineItems = mergedPosts.items.map((post) => buildPostEvent(post, livePrice));

  return {
    target: {
      targetType: target.target_type,
      targetId: target.target_id,
      symbol,
      name,
      chainId: cleanText(target.chain_id),
      address: cleanText(target.address),
      displayTitle: title,
      shortId: shortId(target.target_id),
    },
    route: {
      window: route.window,
      searchHref: `/search?q=${encodeURIComponent(symbol ? `$${symbol}` : target.target_id)}`,
    },
    hero: {
      logoUrl: cleanText(profileIdentity?.logo_url),
      title,
      subtitle: heroSubtitle(dossier),
      contractLabel: target.address
        ? `${target.chain_id ?? "chain"} · ${shortAddress(target.address)}`
        : null,
      actions: heroActions(dossier),
    },
    metrics: tokenCaseMetrics(dossier, route),
    timeline: {
      items: timelineItems,
      hasMore: mergedPosts.has_more,
      isLoading: isLoadingPosts,
      isFetchingNextPage,
      emptyLabel: timelineItems.length ? null : "No matching posts in this window.",
    },
    market,
    dataGaps: [],
  };
}

function tokenCaseMetrics(
  dossier: TokenCaseDossier,
  route: TokenCaseRouteState,
): TokenCaseViewModel["metrics"] {
  const currentRadar = dossier.current_radar;
  const rank = currentRadar?.radar.rank;
  const lane = cleanText(currentRadar?.radar.lane);
  const decision = currentRadar?.factor_snapshot.composite.recommended_decision;
  const rankScore = numberValue(currentRadar?.factor_snapshot.composite.rank_score);
  const listedDetail = `current ${route.window} row`;
  const missingDetail = `no current ${route.window} row`;

  return [
    {
      key: "mentions",
      label: "mentions",
      value: compactNumber(dossier.timeline.summary.posts),
      detail: `${compactNumber(dossier.timeline.summary.authors)} authors`,
      tone: dossier.timeline.summary.posts > 0 ? "health" : "neutral",
    },
    {
      key: "radar-rank",
      label: "radar rank",
      value: rank === null || rank === undefined ? "not listed" : `#${rank}`,
      detail: currentRadar ? listedDetail : missingDetail,
      tone: currentRadar ? "info" : "neutral",
    },
    {
      key: "radar-lane",
      label: "radar lane",
      value: lane ?? "not listed",
      detail: currentRadar ? `quality ${currentRadar.quality.status}` : missingDetail,
      tone: currentRadar ? "info" : "neutral",
    },
    {
      key: "radar-decision",
      label: "radar decision",
      value: decision ?? "not listed",
      detail:
        currentRadar && rankScore !== null
          ? `rank score ${compactNumber(rankScore)}`
          : currentRadar
            ? "rank score unavailable"
            : missingDetail,
      tone:
        decision === "high_alert"
          ? "opportunity"
          : decision === "discard"
            ? "risk"
            : currentRadar
              ? "info"
              : "neutral",
    },
  ];
}

function buildPostEvent(post: TokenPostItem, livePriceUsd: number | null): TokenCasePostEvent {
  const score = numberValue(post.post_quality.score);
  const reasons = post.post_quality.reasons ?? [];
  return {
    id: post.event_id,
    handle: cleanText(post.author_handle),
    text: cleanText(post.text) ?? "(empty post)",
    url: cleanText(post.url),
    timestampMs: post.received_at_ms ?? null,
    timeLabel: post.received_at_ms ? timeAgoLabel(post.received_at_ms) : null,
    phase: cleanText(post.stage_phase),
    role: cleanText(post.author_role),
    pills: postPills(post),
    market: buildPostMarket(post, livePriceUsd),
    quality: {
      score,
      scoreLabel: score === null ? "PQ -" : `PQ ${Math.round(score)}`,
      reasons,
      contributions: post.post_quality.contributions.slice(0, 3).map((contribution) => ({
        label: contribution.feature.replaceAll("_", " "),
        value: formatContributionValue(contribution.value),
        reason: contribution.reason,
      })),
    },
  };
}

function buildMarketView(dossier: TokenCaseDossier): TokenCaseMarketView {
  const live = dossier.market_live;
  const status = stringValue(live.status) ?? "missing";
  const price = numberValue(live.price_usd);
  const marketCap = numberValue(live.market_cap_usd);
  const liquidity = numberValue(live.liquidity_usd);
  const holders = numberValue(live.holders);
  const volume24h = numberValue(live.volume_24h_usd);
  const openInterest = numberValue(live.open_interest_usd);
  const ready = status === "ready" || status === "live";
  return {
    status,
    provider: stringValue(live.provider),
    priceLabel: price === null ? "-" : formatTokenPriceUsd(price),
    marketCapLabel: marketCap === null ? "-" : formatUsdCompact(marketCap),
    liquidityLabel: liquidity === null ? "-" : formatUsdCompact(liquidity),
    holdersLabel: holders === null ? "-" : compactNumber(holders),
    volume24hLabel: volume24h === null ? "-" : formatUsdCompact(volume24h),
    openInterestLabel: openInterest === null ? "-" : formatUsdCompact(openInterest),
    observedAtLabel: numberValue(live.observed_at_ms)
      ? timeAgoLabel(Number(live.observed_at_ms))
      : null,
    emptyTitle: ready ? null : status === "stale" ? "Live market stale" : "Live market unavailable",
    emptyDetail: ready
      ? null
      : (stringValue(live.error) ??
        "No live market snapshot has been attached to this dossier yet."),
    tone: ready ? "health" : "warn",
  };
}

function buildPostMarket(
  post: TokenPostItem,
  livePriceUsd: number | null,
): TokenCasePostEvent["market"] {
  return buildTokenPostEventMarket({
    livePriceUsd,
    observationKind: post.price?.observation_kind,
    priceUsd: post.price?.price_usd,
    provider: post.price?.provider,
    status: post.price?.status,
  });
}

function postPills(post: TokenPostItem): Array<{ label: string; tone: TokenCaseTone }> {
  const pricePill = tokenPricePill(post.price?.price_usd, post.price?.status);
  return pricePill ? [pricePill] : [];
}

function heroSubtitle(dossier: TokenCaseDossier): string {
  const target = dossier.target;
  const parts = [
    target.chain_id,
    target.address ? shortAddress(target.address) : null,
    dossier.profile?.status ? `profile ${dossier.profile.status}` : null,
  ].filter((part): part is string => Boolean(part));
  return parts.join(" · ") || target.target_id;
}

function heroActions(dossier: TokenCaseDossier): TokenCaseViewModel["hero"]["actions"] {
  const actions: TokenCaseViewModel["hero"]["actions"] = [];
  const twitter = cleanText(dossier.profile?.links?.twitter_username);
  const website = cleanText(dossier.profile?.links?.website_url);
  const gmgn = cleanText(dossier.profile?.links?.gmgn_url);
  if (twitter) {
    actions.push({
      label: "X",
      href: `https://x.com/${twitter.replace(/^@+/, "")}`,
      tone: "info",
    });
  }
  if (website) {
    actions.push({ label: "Website", href: website, tone: "neutral" });
  }
  if (gmgn) {
    actions.push({ label: "GMGN", href: gmgn, tone: "opportunity" });
  }
  return actions;
}

function shortId(value: string): string {
  return value.length > 30 ? `${value.slice(0, 14)}...${value.slice(-8)}` : value;
}

function timeAgoLabel(timestampMs: number): string {
  return relativeTimeLabel(timestampMs);
}

function formatContributionValue(value: number): string {
  return Number.isFinite(value) ? value.toFixed(2) : "-";
}

function stringValue(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}
