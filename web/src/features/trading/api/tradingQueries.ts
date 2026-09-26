import { getApi } from "@lib/api/client";
import type { components } from "@lib/types/openapi";
import { queryKeys } from "@shared/query/queryKeys";
import { useQuery } from "@tanstack/react-query";

type TradingSchemas = components["schemas"];

export type TradingStatus = TradingSchemas["TradingStatusData"];
export type TradingExecutionReadiness = TradingSchemas["TradingExecutionReadinessData"];
export type TradingCases = TradingSchemas["TradingCasesData"];
export type TradingCase = TradingSchemas["TradingCaseData"];
export type TradingPolicyCheck = TradingSchemas["TradingPolicyCheckData"];
export type TradingExecutions = TradingSchemas["TradingExecutionsData"];
export type TradingExecutionRow = TradingSchemas["TradingExecutionRowData"];
export type TradingRealizedTotals = TradingSchemas["TradingRealizedTotalsData"];
export type TradingAdmissionCount = TradingSchemas["TradingAdmissionCountData"];
export type TradingAnalysisReplay = TradingSchemas["TradingAnalysisReplayData"];

export const TRADING_REFETCH_MS = 15_000;
// Runtime heartbeat facts last at most 5 s; live status must refresh before that budget ends.
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

/**
 * The 24 h admission, Case, and Agent-decision distributions, and nothing else.
 *
 * Without `case_id` the response's `cases[]` is empty by contract, so this poll carries three compact
 * count distributions instead of up to 100 frozen Cases with their checks attached — of which the desk could
 * render at most one, once a reader clicked. The one Case a reader does click is the query below.
 */
export const useTradingCasesWithToken = (token: string) =>
  useQuery({
    enabled: Boolean(token),
    queryKey: queryKeys.tradingCases(""),
    queryFn: async () =>
      (
        await getApi<TradingCases>("/api/trading/cases", {
          etagKey: "trading-cases",
          token,
        })
      ).data,
    refetchInterval: TRADING_REFETCH_MS,
    staleTime: 5_000,
  });

/**
 * One Case by primary key, read only while the drawer behind `?case=<id>` is open.
 *
 * Frozen inputs remain fixed while attempts, WATCH observations and child Cases
 * can still arrive. An unknown id answers with an empty `cases[]`.
 */
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

export const useTradingAnalysisReplay = (
  token: string,
  caseId: string,
  enabled: boolean,
  attempt?: number,
) =>
  useQuery({
    enabled: Boolean(token && caseId && enabled),
    queryKey: [...queryKeys.tradingCases(caseId), "replay", attempt ?? "latest"],
    queryFn: async () =>
      (
        await getApi<TradingAnalysisReplay>(`/api/trading/cases/${caseId}/replay`, {
          etagKey: `trading-case-replay:${caseId}:${attempt ?? "latest"}`,
          params: { attempt },
          token,
        })
      ).data,
    staleTime: 60_000,
  });

/**
 * The desk's execution read model (#528): one row per entry, one row per Command, both already folded.
 *
 * An entry is a Signal or the manual entry an operator typed, and `source` is what tells the two apart;
 * the server folds each under the identity its own venue observations carry (#528 PR-3). This replaces
 * the raw Observation stream this page used to correlate in the browser. That correlation was wrong for
 * a flatten — the exit orders carry the *entry's* id, not the flatten Command's — and `stage` is now the
 * server's word from `tracefold/trading/stages.py`, so the CLI and the console cannot disagree about how
 * far one entry got. `totals` is the same ledger's realized sum over today and over all time (#604 T3):
 * the desk states the two numbers the server added up rather than summing the rows it happens to hold.
 */
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

export const useTradingCaseListWithToken = (
  token: string,
  filters: Record<string, string | undefined>,
) =>
  useQuery({
    enabled: Boolean(token),
    queryKey: [...queryKeys.tradingCases("browse"), filters],
    queryFn: async () =>
      (
        await getApi<TradingCases>("/api/trading/cases", {
          etagKey: `trading-case-list:${JSON.stringify(filters)}`,
          params: { ...filters, limit: 25 },
          token,
        })
      ).data,
    refetchInterval: filters.cursor ? false : TRADING_REFETCH_MS,
    staleTime: 5_000,
  });
