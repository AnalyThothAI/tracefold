import { getApi } from "@lib/api/client";
import type { components } from "@lib/types/openapi";
import { queryKeys } from "@shared/query/queryKeys";
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";

type TradingSchemas = components["schemas"];

export type TradingStatus = TradingSchemas["TradingStatusData"];
export type TradingBudget = TradingSchemas["TradingBudgetData"];
export type TradingDecisionRuntime = TradingSchemas["TradingDecisionRuntimeData"];
export type TradingCapitalRuntime = TradingSchemas["TradingCapitalRuntimeData"];
export type TradingBindingRuntime = TradingSchemas["TradingBindingRuntimeData"];
export type TradingRuntimeCounts = TradingSchemas["TradingRuntimeCountsData"];
export type TradingPolicyIdentity = TradingSchemas["TradingPolicyIdentityData"];
export type TradingIntents = TradingSchemas["TradingIntentsData"];
export type TradingIntent = TradingSchemas["TradingIntentData"];
export type TradingCases = TradingSchemas["TradingCasesData"];
export type TradingCase = TradingSchemas["TradingCaseData"];
export type TradingPolicyCheck = TradingSchemas["TradingPolicyCheckData"];
export type TradingGate = TradingSchemas["TradingGateData"];
export type TradingGateSource = TradingSchemas["TradingGateSourceData"];
export type TradingGateDecision = TradingSchemas["TradingGateDecisionData"];
export type TradingGateConfig = TradingSchemas["TradingGateConfigData"];
export type TradingCapabilities = TradingSchemas["TradingCapabilitiesData"];
export type TradingCapabilityBinding = TradingSchemas["TradingCapabilityBindingData"];
export type TradingCapabilityEntry = TradingSchemas["TradingCapabilityEntryData"];
export type TradingEvidence = TradingSchemas["TradingEvidenceData"];
export type TradingAuthorityEvidence = TradingSchemas["TradingAuthorityEvidenceData"];
export type TradingCapitalLifecycleEvidence = TradingSchemas["TradingCapitalLifecycleEvidenceData"];

/**
 * The capital lane moves at the speed of a frame, not of a price feed. 15 s is the same rhythm the status
 * route uses, and nothing on this surface changes faster than an Intent does.
 */
export const TRADING_REFETCH_MS = 15_000;

/**
 * One hook per durable aggregate (#331), because one route per durable aggregate.
 *
 * The mixed hook these replace fetched Intents and got Cases back in the same payload, so a page could
 * not tell "no Intent" from "no Case" — and a failed request fell through an empty array into a state
 * that reads as "the system had nothing to do".
 */
export const useTradingStatusWithToken = (token: string) =>
  useQuery({
    enabled: Boolean(token),
    queryKey: queryKeys.tradingStatus(),
    queryFn: async () =>
      (
        await getApi<TradingStatus>("/api/trading/status", {
          etagKey: "trading-status",
          token,
        })
      ).data,
    refetchInterval: TRADING_REFETCH_MS,
    staleTime: 5_000,
  });

/** Immutable capital requests and their execution outcomes. Never Cases. */
export const useTradingIntentsWithToken = (token: string, underlying?: string) =>
  useQuery({
    enabled: Boolean(token),
    queryKey: queryKeys.tradingIntents(underlying ?? ""),
    queryFn: async () =>
      (
        await getApi<TradingIntents>("/api/trading/intents", {
          etagKey: `trading-intents:${underlying ?? "all"}`,
          params: underlying ? { underlying } : undefined,
          token,
        })
      ).data,
    refetchInterval: TRADING_REFETCH_MS,
    staleTime: 5_000,
  });

/** Frozen Cases and the frozen evidence each was decided on. */
export const useTradingCasesWithToken = (token: string, underlying?: string) =>
  useQuery({
    enabled: Boolean(token),
    queryKey: queryKeys.tradingCases(underlying ?? ""),
    queryFn: async () =>
      (
        await getApi<TradingCases>("/api/trading/cases", {
          etagKey: `trading-cases:${underlying ?? "all"}`,
          params: underlying ? { underlying } : undefined,
          token,
        })
      ).data,
    refetchInterval: TRADING_REFETCH_MS,
    staleTime: 5_000,
  });

/** Complete V2 included/excluded partitions, page-by-page from the durable projection. */
export const useTradingCapabilitiesWithToken = (token: string) =>
  useInfiniteQuery<TradingCapabilities>({
    enabled: Boolean(token),
    getNextPageParam: (lastPage) =>
      lastPage.complete ? undefined : (lastPage.next_cursor ?? undefined),
    initialPageParam: "",
    queryFn: async ({ pageParam }): Promise<TradingCapabilities> => {
      const cursor = typeof pageParam === "string" ? pageParam : "";
      return (
        await getApi<TradingCapabilities>("/api/trading/capabilities", {
          etagKey: `trading-capabilities:${cursor || "first"}`,
          params: cursor ? { cursor } : undefined,
          token,
        })
      ).data;
    },
    queryKey: queryKeys.tradingCapabilities(),
    refetchInterval: TRADING_REFETCH_MS,
    staleTime: 5_000,
  });

/** Redacted authority chain and reservation/Intent lifecycle evidence; never provider I/O. */
export const useTradingEvidenceWithToken = (token: string) =>
  useInfiniteQuery<TradingEvidence>({
    enabled: Boolean(token),
    getNextPageParam: (lastPage) =>
      lastPage.complete ? undefined : (lastPage.next_cursor ?? undefined),
    initialPageParam: "",
    queryFn: async ({ pageParam }): Promise<TradingEvidence> => {
      const cursor = typeof pageParam === "string" ? pageParam : "";
      return (
        await getApi<TradingEvidence>("/api/trading/evidence", {
          etagKey: `trading-evidence:${cursor || "first"}`,
          params: cursor ? { cursor } : undefined,
          token,
        })
      ).data;
    },
    queryKey: queryKeys.tradingEvidence(),
    refetchInterval: TRADING_REFETCH_MS,
    staleTime: 5_000,
  });

/**
 * Every admission answer in the window, in one read (#269).
 *
 * The per-Source endpoint below answers the same question for one Event, which is what an Event detail
 * asks. A frame table asks it for a page of frames at once, and a hundred round trips to render one
 * screen is why the column said 未成案 with no reason for every row while the ledger held one for each.
 */
export const useTradingGateWithToken = (token: string) =>
  useQuery({
    enabled: Boolean(token),
    queryKey: queryKeys.tradingGate(),
    queryFn: async () =>
      (
        await getApi<TradingGate>("/api/trading/gate", {
          etagKey: "trading-gate",
          token,
        })
      ).data,
    refetchInterval: TRADING_REFETCH_MS,
    staleTime: 5_000,
  });

/**
 * What admission decided about one Event's Source.
 *
 * `lane` is required rather than inferred, and only `oi` is a real question. The deterministic lane's
 * source key is `oi:{event_id}:{metric_version}`; nothing else is reconstructible from an Event id, and
 * the server answers `joinable: false` for those rather than reporting a refusal that never happened.
 */
export const useTradingGateSourceWithToken = (
  token: string,
  eventId: string | null | undefined,
  lane: "oi" | "news",
) =>
  useQuery({
    enabled: Boolean(token && eventId && lane === "oi"),
    queryKey: queryKeys.tradingGateSource(eventId ?? ""),
    queryFn: async () =>
      (
        await getApi<TradingGateSource>(`/api/trading/gate/${encodeURIComponent(eventId ?? "")}`, {
          etagKey: `trading-gate-source:${eventId}`,
          params: { lane: "oi" },
          token,
        })
      ).data,
    staleTime: TRADING_REFETCH_MS,
  });
