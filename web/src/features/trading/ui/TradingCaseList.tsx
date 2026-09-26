import { ActionButton } from "@shared/ui/ActionButton";
import { Card } from "@shared/ui/Card";
import { EmptyNote } from "@shared/ui/EmptyNote";
import * as PageState from "@shared/ui/PageState";
import { useSearchParams } from "react-router-dom";

import { useTradingCaseListWithToken } from "../api/tradingQueries";
import { caseVerdict } from "../model/tradingCases";
import { caseClock } from "../model/tradingLabels";

export function TradingCaseList({
  token,
  onOpen,
}: {
  token: string;
  onOpen: (id: string) => void;
}) {
  const [params, setParams] = useSearchParams();
  const requestedState = params.get("state");
  const defaultAgentView =
    !requestedState && !params.has("source_item_id") && !params.has("reason");
  const filters = {
    view: "list",
    state:
      requestedState === "ALL"
        ? undefined
        : requestedState || (defaultAgentView ? "DONE" : undefined),
    reason: params.get("reason") || undefined,
    asset: params.get("asset") || undefined,
    source_item_id: params.get("source_item_id") || undefined,
    cursor: params.get("cursor") || undefined,
  };
  const query = useTradingCaseListWithToken(token, filters);
  const data = query.data;
  return (
    <Card
      title={filters.source_item_id ? "这条观察关联的策略判定" : "Case 与 Agent 判定"}
      hint="逐条核对冻结事实、Agent 判断与信号发布状态。"
      flush
    >
      <form
        className="trading-research-toolbar"
        onSubmit={(event) => {
          event.preventDefault();
          const form = new FormData(event.currentTarget);
          const next = new URLSearchParams(params);
          const asset = String(form.get("asset") ?? "")
            .trim()
            .toUpperCase();
          if (asset) next.set("asset", asset);
          else next.delete("asset");
          next.delete("cursor");
          setParams(next);
        }}
      >
        <label>
          品种
          <input
            name="asset"
            placeholder="如 BTC"
            defaultValue={filters.asset}
            key={filters.asset}
          />
        </label>
        <label>
          判定状态
          <select
            value={requestedState ?? (defaultAgentView ? "DONE" : "ALL")}
            onChange={(event) => {
              const next = new URLSearchParams(params);
              next.set("state", event.target.value);
              next.delete("cursor");
              setParams(next);
            }}
          >
            <option value="ALL">全部状态</option>
            <option value="NO_TRADE">不交易</option>
            <option value="DONE">Agent 已判断</option>
            <option value="FAILED">分析不可用</option>
            <option value="EXCLUDED">目标排除</option>
            <option value="SIGNAL_EMITTED">已发出信号</option>
            <option value="BLOCKED">判定受阻</option>
            <option value="PENDING">等待判定</option>
            <option value="RUNNING">正在判定</option>
          </select>
        </label>
        <ActionButton type="submit" size="sm">
          筛选
        </ActionButton>
        {filters.reason || filters.source_item_id || filters.asset || requestedState ? (
          <ActionButton
            size="sm"
            onClick={() => {
              const next = new URLSearchParams(params);
              ["state", "asset", "reason", "source_item_id", "cursor", "case"].forEach((key) =>
                next.delete(key),
              );
              next.set("tab", "decisions");
              setParams(next);
            }}
          >
            清除筛选
          </ActionButton>
        ) : null}
      </form>
      {query.isError && !data ? (
        <PageState.Error error={query.error} onRetry={() => void query.refetch()} />
      ) : !data ? (
        <PageState.Loading layout="panel" label="正在读取策略判定" rows={4} />
      ) : (
        <PageState.Stale
          failedRefresh={query.isError ? "策略判定刷新失败，保留上次记录。" : undefined}
          onRetry={() => void query.refetch()}
          updating={query.isFetching}
        >
          {filters.reason ? (
            <p className="trading-inline-empty">原因筛选：{filters.reason}</p>
          ) : null}
          <p className="trading-inline-empty">
            {filters.source_item_id
              ? "按保存的来源身份关联，覆盖保留期；未匹配不代表未评估。"
              : `${filters.state === "DONE" ? "Agent 已判断" : "全部 Case"} · 最近 24 小时 · 按创建时间排序`}{" "}
            · 共 {data.total} 条
          </p>
          {(data.cases ?? []).length ? (
            <div className="trading-case-list">
              {(data.cases ?? []).map((item) => (
                <button
                  type="button"
                  className="trading-case-list-row"
                  key={item.case_id}
                  onClick={() => onOpen(item.case_id)}
                >
                  <span className="trading-case-identity">
                    <b>{item.base_symbol.length > 24 ? "待映射标的" : item.base_symbol}</b>
                    <small>
                      {item.trigger_kind === "oi"
                        ? "OI 触发"
                        : item.trigger_kind
                          ? `${item.trigger_kind.toUpperCase()} 触发`
                          : "策略 Case"}
                    </small>
                  </span>
                  <span
                    className="trading-case-result"
                    data-action={item.analysis_action ?? undefined}
                  >
                    {caseVerdict(item)}
                  </span>
                  <span className="trading-case-open">
                    <small>{caseClock(item.created_at_ms)}</small>
                    <span>查看依据 ↗</span>
                  </span>
                </button>
              ))}
            </div>
          ) : (
            <EmptyNote>
              {filters.source_item_id
                ? "暂无可关联的策略判定；不按币种或相近时间猜测关联。"
                : "当前筛选下没有策略判定。"}
            </EmptyNote>
          )}
          <div className="trading-research-toolbar">
            {filters.cursor ? (
              <ActionButton
                size="sm"
                onClick={() => {
                  const next = new URLSearchParams(params);
                  next.delete("cursor");
                  setParams(next);
                }}
              >
                回到首屏
              </ActionButton>
            ) : null}
            {data.next_cursor ? (
              <ActionButton
                size="sm"
                onClick={() => {
                  const next = new URLSearchParams(params);
                  next.set("cursor", data.next_cursor!);
                  setParams(next);
                }}
              >
                下一页
              </ActionButton>
            ) : null}
          </div>
        </PageState.Stale>
      )}
    </Card>
  );
}
