import { getApi } from "@lib/api/client";
import type { components } from "@lib/types/openapi";
import { queryKeys } from "@shared/query/queryKeys";
import { useQuery } from "@tanstack/react-query";

type TradingSchemas = components["schemas"];

export type TradingStatus = TradingSchemas["TradingStatusData"];
export type TradingBudget = TradingSchemas["TradingBudgetData"];
export type TradingReadiness = TradingSchemas["TradingReadinessData"];
export type TradingFloors = TradingSchemas["TradingFloorsData"];
export type TradingCounts = TradingSchemas["TradingCountsData"];
export type TradingOrders = TradingSchemas["TradingOrdersData"];
export type TradingOrder = TradingSchemas["TradingOrderData"];
export type TradingCase = TradingSchemas["TradingCaseData"];
export type TradingEventCase = TradingSchemas["TradingEventCaseData"];

/**
 * The capital lane moves at the speed of a frame, not of a price feed. 15 s is the same rhythm the status
 * route uses, and there is nothing on this surface that changes faster than an order does.
 */
export const TRADING_REFETCH_MS = 15_000;

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

export const useTradingOrdersWithToken = (token: string, underlying?: string) =>
  useQuery({
    enabled: Boolean(token),
    queryKey: queryKeys.tradingOrders(underlying ?? ""),
    queryFn: async () =>
      (
        await getApi<TradingOrders>("/api/trading/orders", {
          etagKey: `trading-orders:${underlying ?? "all"}`,
          params: underlying ? { underlying } : undefined,
          token,
        })
      ).data,
    refetchInterval: TRADING_REFETCH_MS,
    staleTime: 5_000,
  });

/**
 * Whether one Event became a case (#207 PR-W4).
 *
 * `lane` is required rather than inferred, and only `oi` is a real question. The deterministic lane's source
 * key is `oi:{event_id}:{metric_version}`; the model lane's is a content hash of an artifact and a
 * fingerprint (#154), which no Event id reconstructs. The server answers `joinable: false` for anything
 * else, and the badge renders that as "cannot be asked" rather than as "no".
 */
export const useTradingEventCaseWithToken = (
  token: string,
  eventId: string | null | undefined,
  lane: "oi" | "news",
) =>
  useQuery({
    enabled: Boolean(token && eventId && lane === "oi"),
    queryKey: queryKeys.tradingEventCase(eventId ?? ""),
    queryFn: async () =>
      (
        await getApi<TradingEventCase>(`/api/trading/events/${encodeURIComponent(eventId ?? "")}`, {
          etagKey: `trading-event-case:${eventId}`,
          params: { lane: "oi" },
          token,
        })
      ).data,
    staleTime: TRADING_REFETCH_MS,
  });
