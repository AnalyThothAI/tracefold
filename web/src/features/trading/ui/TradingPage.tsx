import { useMediaQuery } from "@shared/hooks/useMediaQuery";
import { ActionButton } from "@shared/ui/ActionButton";
import { Drawer } from "@shared/ui/Drawer";
import { EmptyNote } from "@shared/ui/EmptyNote";
import { PageHeader } from "@shared/ui/PageHeader";
import { PageShell } from "@shared/ui/PageShell";
import * as PageState from "@shared/ui/PageState";
import { useRef } from "react";
import { useSearchParams } from "react-router-dom";

import {
  useTradingCaseWithToken,
  useTradingExecutionsWithToken,
  useTradingStatusWithToken,
} from "../api/tradingQueries";
import { caseClock } from "../model/tradingLabels";
import { useTradingFactExpiry } from "../state/useTradingFactExpiry";

import { TradingCaseDetail } from "./TradingCaseDetail";
import { TradingCaseList } from "./TradingCaseList";
import { TradingLoopLedger } from "./TradingLoopLedger";
import { TradingExposure, TradingSafetyStrip } from "./TradingRisk";
import { TradingScoreboard } from "./TradingScoreboard";
import { TradingTally } from "./TradingTally";

import "./trading.css";

type Tab = "positions" | "scoreboard" | "cases" | "executions";

export function TradingPage({ token }: { token: string }) {
  const [params, setParams] = useSearchParams();
  const wide = useMediaQuery("(min-width: 1100px)");
  const opener = useRef<HTMLElement | null>(null);
  const tab: Tab = ["scoreboard", "cases", "executions"].includes(params.get("tab") ?? "")
    ? (params.get("tab") as Tab)
    : "positions";
  const selectedCaseId = params.get("case");
  const sourceItemId = params.get("source_item_id") || undefined;
  const statusQuery = useTradingStatusWithToken(token);
  const executionCase = params.get("execution_case") || undefined;
  const executionsQuery = useTradingExecutionsWithToken(
    token,
    tab === "executions" ? executionCase : undefined,
  );
  const caseQuery = useTradingCaseWithToken(token, selectedCaseId);
  const status = statusQuery.data;
  const executions = executionsQuery.data?.executions ?? [];
  const stale = useTradingFactExpiry(
    status?.execution.facts_expire_at_ms,
    status?.execution.facts_remaining_ms,
  );
  const selectedCase = selectedCaseId
    ? caseQuery.data?.cases?.find((item) => item.case_id === selectedCaseId)
    : undefined;

  const selectCase = (caseId: string | null) => {
    const next = new URLSearchParams(params);
    if (caseId) {
      opener.current =
        document.activeElement instanceof HTMLElement ? document.activeElement : null;
      next.set("case", caseId);
    } else next.delete("case");
    setParams(next, { replace: true });
  };
  const selectTab = (value: Tab) => {
    const next = new URLSearchParams(params);
    next.set("tab", value);
    next.delete("case");
    next.delete("execution_case");
    next.delete("source_item_id");
    setParams(next);
  };

  const detail = selectedCaseId ? (
    <Drawer
      title="冻结 Case"
      open
      inline={wide}
      modal={false}
      width={680}
      restoreFocusTo={opener.current}
      onOpenChange={(open) => {
        if (!open) selectCase(null);
      }}
      actions={
        <ActionButton size="sm" onClick={() => selectCase(null)}>
          关闭
        </ActionButton>
      }
    >
      {selectedCase ? (
        <TradingCaseDetail item={selectedCase} />
      ) : (
        <EmptyNote>{caseQuery.isPending ? "正在读取 Case" : "未找到 Case"}</EmptyNote>
      )}
    </Drawer>
  ) : null;

  return (
    <PageShell archetype="scan" className="trading-shell" label="交易执行监控">
      <PageHeader title="交易执行" subtitle="LIVE 行情评估与 DEMO 账户执行分开记录。" />
      {statusQuery.isPending && !status ? (
        <PageState.Loading label="正在读取交易台" layout="panel" rows={4} />
      ) : statusQuery.isError && !status ? (
        <PageState.Error error={statusQuery.error} onRetry={() => void statusQuery.refetch()} />
      ) : (
        <div className="trading-body">
          {status ? <TradingSafetyStrip execution={status.execution} stale={stale} /> : null}
          <div className="trading-research-tabs" role="group" aria-label="交易视图">
            {(
              [
                ["positions", "持仓与订单"],
                ["scoreboard", "策略记分板"],
                ["cases", "冻结 Case"],
                ["executions", "执行记录"],
              ] as const
            ).map(([value, label]) => (
              <button
                type="button"
                key={value}
                aria-pressed={tab === value}
                data-active={tab === value || undefined}
                onClick={() => selectTab(value)}
              >
                {label}
              </button>
            ))}
          </div>
          <div className="trading-workbench" data-split={!!selectedCaseId || undefined}>
            <div className="trading-workbench-main">
              {tab === "positions" ? (
                <>
                  {status ? <TradingExposure execution={status.execution} stale={stale} /> : null}
                  <TradingTally
                    execution={status?.execution}
                    executions={executions}
                    executionsFailed={executionsQuery.isError}
                    executionsPending={executionsQuery.isPending}
                    totals={executionsQuery.data?.totals}
                    stale={stale}
                  />
                </>
              ) : tab === "scoreboard" ? (
                <TradingScoreboard token={token} />
              ) : tab === "cases" ? (
                <TradingCaseList
                  token={token}
                  onOpen={selectCase}
                  sourceItemId={sourceItemId}
                  onClearSource={() => {
                    const next = new URLSearchParams(params);
                    next.delete("source_item_id");
                    setParams(next);
                  }}
                />
              ) : (
                <TradingLoopLedger
                  caseFiltered={!!executionCase}
                  complete={executionsQuery.data?.complete ?? true}
                  failed={executionsQuery.isError}
                  onOpenCase={selectCase}
                  pending={executionsQuery.isPending}
                  rows={executions}
                  selectedCaseId={selectedCaseId}
                />
              )}
            </div>
            {detail}
          </div>
          <p className="trading-routes-line">
            分析 {status?.decision.state ?? "未取得"} · 最近 Case{" "}
            {caseClock(status?.decision.last_case_at_ms)}
            {status?.decision.fault_code ? ` · ${status.decision.fault_code}` : ""}
            {status?.decision.program_sha
              ? ` · 程序 ${status.decision.program_sha.slice(0, 12)}`
              : ""}
            {status?.decision.publish_signals ? " · 发布已开启" : " · 只观察"}
          </p>
        </div>
      )}
    </PageShell>
  );
}
