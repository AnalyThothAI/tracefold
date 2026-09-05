import { getApi, postApi } from "@lib/api/client";
import type { components } from "@lib/types/openapi";
import { queryKeys } from "@shared/query/queryKeys";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

type TradingSchemas = components["schemas"];

export type TradingStatus = TradingSchemas["TradingStatusData"];
export type TradingExecutionReadiness = TradingSchemas["TradingExecutionReadinessData"];
export type TradingCases = TradingSchemas["TradingCasesData"];
export type TradingCase = TradingSchemas["TradingCaseData"];
export type TradingPolicyCheck = TradingSchemas["TradingPolicyCheckData"];
export type TradingExecutions = TradingSchemas["TradingExecutionsData"];
export type TradingExecutionRow = TradingSchemas["TradingExecutionRowData"];
export type TradingExecutionCommand = TradingSchemas["TradingExecutionCommandRowData"];
export type TradingOperatorCommandReceipt = TradingSchemas["TradingOperatorCommandReceiptData"];

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

/**
 * The desk's execution read model (#528): one row per entry, one row per Command, both already folded.
 *
 * An entry is a Signal or the manual entry an operator typed, and `source` is what tells the two apart;
 * the server folds each under the identity its own venue observations carry (#528 PR-3). This replaces
 * the raw Observation stream this page used to correlate in the browser. That correlation was wrong for
 * a flatten — the exit orders carry the *entry's* id, not the flatten Command's — and `stage` is now the
 * server's word from `tracefold/trading/stages.py`, so the CLI and the console cannot disagree about how
 * far one entry got.
 */
export const useTradingExecutionsWithToken = (token: string) =>
  useQuery({
    enabled: Boolean(token),
    queryKey: queryKeys.tradingExecutions(),
    queryFn: async () =>
      (
        await getApi<TradingExecutions>("/api/trading/executions", {
          etagKey: "trading-executions",
          token,
        })
      ).data,
    refetchInterval: TRADING_REFETCH_MS,
    staleTime: 5_000,
  });

export const useIssueTradingCommandWithToken = (token: string) => {
  const queryClient = useQueryClient();
  return useMutation({
    mutationKey: ["trading-operator-command"],
    mutationFn: async (command: { requestId: string; requestedAtMs: number; text: string }) =>
      (
        await postApi<TradingOperatorCommandReceipt>("/api/trading/execution/commands", {
          body: {
            request_id: command.requestId,
            requested_at_ms: command.requestedAtMs,
            text: command.text,
          },
          token,
        })
      ).data,
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: queryKeys.tradingExecutions() }),
        queryClient.invalidateQueries({ queryKey: queryKeys.tradingStatus() }),
      ]);
    },
    retry: false,
  });
};
