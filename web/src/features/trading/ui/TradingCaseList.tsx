import { Card } from "@shared/ui/Card";
import { EmptyNote } from "@shared/ui/EmptyNote";
import * as PageState from "@shared/ui/PageState";
import { useState } from "react";

import { useTradingCasesWithToken } from "../api/tradingQueries";
import { caseClock } from "../model/tradingLabels";

export function TradingCaseList({
  token,
  onOpen,
  sourceItemId,
  onClearSource,
}: {
  token: string;
  onOpen: (id: string) => void;
  sourceItemId?: string;
  onClearSource: () => void;
}) {
  const [state, setState] = useState("");
  const query = useTradingCasesWithToken(token, state || undefined, sourceItemId);
  const data = query.data;
  return (
    <Card flush title="冻结 Case" hint="仅显示选中的来源；排除原因保存在 Trigger">
      {sourceItemId ? (
        <p className="trading-routes-line">
          按这条 OI 观察筛选 ·{" "}
          <button type="button" onClick={onClearSource}>
            显示全部 Case
          </button>
        </p>
      ) : null}
      <label className="trading-case-filter">
        状态{" "}
        <select value={state} onChange={(event) => setState(event.target.value)}>
          <option value="">全部</option>
          <option value="pending">等待</option>
          <option value="running">评估中</option>
          <option value="complete">完成</option>
          <option value="failed">失败</option>
        </select>
      </label>
      {query.isError && !data ? (
        <PageState.Error error={query.error} onRetry={() => void query.refetch()} />
      ) : !data ? (
        <PageState.Loading label="正在读取 Case" layout="panel" rows={4} />
      ) : data.cases?.length ? (
        <div className="trading-case-list">
          {data.cases.map((item) => (
            <button
              type="button"
              className="trading-case-list-row"
              key={item.case_id}
              onClick={() => onOpen(item.case_id)}
            >
              <strong>{item.asset_id}</strong>
              <span>{item.trigger_kind}</span>
              <span>
                {item.state}
                {item.failure_code ? ` · ${item.failure_code}` : ""}
              </span>
              <small>{caseClock(item.created_at_ms)}</small>
            </button>
          ))}
        </div>
      ) : (
        <EmptyNote>
          {sourceItemId ? "这条观察没有匹配的冻结 Case。" : "最近 24 小时无匹配 Case。"}
        </EmptyNote>
      )}
    </Card>
  );
}
