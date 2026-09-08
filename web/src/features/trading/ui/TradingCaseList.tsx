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
  const filters = {
    view: "list",
    state: params.get("state") || undefined,
    reason: params.get("reason") || undefined,
    asset: params.get("asset") || undefined,
    source_item_id: params.get("source_item_id") || undefined,
    cursor: params.get("cursor") || undefined,
  };
  const query = useTradingCaseListWithToken(token, filters);
  const data = query.data;
  return (
    <Card
      title={filters.source_item_id ? "这条观察关联的策略判定" : "策略判定记录"}
      hint="点击一条判定，核对当时冻结的条件和实测值。"
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
            value={filters.state ?? ""}
            onChange={(event) => {
              const next = new URLSearchParams(params);
              if (event.target.value) next.set("state", event.target.value);
              else next.delete("state");
              next.delete("cursor");
              setParams(next);
            }}
          >
            <option value="">全部状态</option>
            <option value="NO_TRADE">不交易</option>
            <option value="SIGNAL_EMITTED">已发出信号</option>
            <option value="BLOCKED">判定受阻</option>
            <option value="PENDING">等待判定</option>
            <option value="RUNNING">正在判定</option>
          </select>
        </label>
        <ActionButton type="submit" size="sm">
          筛选
        </ActionButton>
        {filters.reason || filters.source_item_id || filters.asset || filters.state ? (
          <ActionButton
            size="sm"
            onClick={() => {
              const next = new URLSearchParams();
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
              : "最近 24 小时 · 按创建时间排序"}{" "}
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
                  <b>{item.base_symbol}</b>
                  <span>{caseVerdict(item)}</span>
                  <small>{caseClock(item.created_at_ms)} · 查看依据</small>
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
