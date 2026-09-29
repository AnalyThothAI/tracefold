import { getApi } from "@lib/api/client";
import type { components } from "@lib/types/openapi";
import { queryKeys } from "@shared/query/queryKeys";
import { useQuery } from "@tanstack/react-query";

type TradingSchemas = components["schemas"];

export type TradingStatus = TradingSchemas["TradingStatusData"];
export type TradingExecutionReadiness = TradingSchemas["TradingExecutionReadinessData"];
export type TradingCases = TradingSchemas["TradingCasesData"];
export type TradingCase = TradingSchemas["TradingCaseData"];
export type TradingScoreboard = TradingSchemas["TradingScoreboardData"];
export type TradingExecutions = TradingSchemas["TradingExecutionsData"];
export type TradingExecutionRow = TradingSchemas["TradingExecutionRowData"];
export type TradingRealizedTotals = TradingSchemas["TradingRealizedTotalsData"];

export const TRADING_REFETCH_MS = 15_000;
export const TRADING_STATUS_REFETCH_MS = 1_000;

export const useTradingStatusWithToken = (token: string) =>
  useQuery({
    enabled: Boolean(token),
    queryKey: queryKeys.tradingStatus(),
    queryFn: async () => {
      const started = performance.now();
      const status = (await getApi<TradingStatus>("/api/trading/status", { token })).data;
      const elapsedMs = Math.ceil(performance.now() - started);
      return {
        ...status,
        execution: {
          ...status.execution,
          facts_remaining_ms:
            status.execution.facts_remaining_ms == null
              ? null
              : Math.max(0, status.execution.facts_remaining_ms - elapsedMs),
        },
      };
    },
    refetchInterval: TRADING_STATUS_REFETCH_MS,
    staleTime: 0,
    refetchOnWindowFocus: "always",
  });

export const useTradingCasesWithToken = (token: string, state?: string, sourceItemId?: string) =>
  useQuery({
    enabled: Boolean(token),
    queryKey: [...queryKeys.tradingCases("list"), state ?? "", sourceItemId ?? ""],
    queryFn: async () =>
      (
        await getApi<TradingCases>("/api/trading/cases", {
          etagKey: `trading-cases:${state ?? ""}:${sourceItemId ?? ""}`,
          params: { state, source_item_id: sourceItemId, limit: 25 },
          token,
        })
      ).data,
    refetchInterval: TRADING_REFETCH_MS,
    staleTime: 5_000,
  });

export const useTradingCaseWithToken = (token: string, caseId: string | null) =>
  useQuery({
    enabled: Boolean(token && caseId),
    queryKey: queryKeys.tradingCases(caseId ?? ""),
    queryFn: async () =>
      (
        await getApi<TradingCases>("/api/trading/cases", {
          etagKey: `trading-case:${caseId}`,
          params: { case_id: caseId },
          token,
        })
      ).data,
    staleTime: 5_000,
    refetchInterval: TRADING_REFETCH_MS,
  });

export const useTradingScoreboardWithToken = (token: string) =>
  useQuery({
    enabled: Boolean(token),
    queryKey: ["trading", "scoreboard"],
    queryFn: async () =>
      (
        await getApi<TradingScoreboard>("/api/trading/scoreboard", {
          etagKey: "trading-scoreboard",
          token,
        })
      ).data,
    refetchInterval: TRADING_REFETCH_MS,
    staleTime: 5_000,
  });

export const useTradingExecutionsWithToken = (token: string, caseId?: string) =>
  useQuery({
    enabled: Boolean(token),
    queryKey: [...queryKeys.tradingExecutions(), caseId ?? ""],
    queryFn: async () =>
      (
        await getApi<TradingExecutions>("/api/trading/executions", {
          etagKey: `trading-executions:${caseId ?? ""}`,
          params: { case_id: caseId },
          token,
        })
      ).data,
    refetchInterval: TRADING_REFETCH_MS,
    staleTime: 5_000,
  });
