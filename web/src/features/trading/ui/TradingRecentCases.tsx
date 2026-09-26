import { ActionButton } from "@shared/ui/ActionButton";
import { Card } from "@shared/ui/Card";
import { EmptyNote } from "@shared/ui/EmptyNote";

import { useTradingCaseListWithToken } from "../api/tradingQueries";
import { caseVerdict } from "../model/tradingCases";
import { caseClock, ledgerSentence } from "../model/tradingLabels";

/** Independent recent decisions, never a symbol-based attribution of the positions beside them. */
export function TradingRecentCases({
  token,
  onOpen,
  onBrowse,
}: {
  token: string;
  onOpen: (caseId: string) => void;
  onBrowse: () => void;
}) {
  const query = useTradingCaseListWithToken(token, { view: "list", state: "DONE" });
  const items = query.data?.cases?.slice(0, 3) ?? [];
  return (
    <Card className="trading-recent" flush title="最近策略判定" hint="近 24 小时 · 独立记录">
      {items.length ? (
        items.map((item) => (
          <button
            className="trading-recent-row"
            key={item.case_id}
            type="button"
            onClick={() => onOpen(item.case_id)}
          >
            <span>
              <b>{item.base_symbol}</b>
              <small>{caseClock(item.decided_at_ms ?? item.observed_at_ms)}</small>
            </span>
            <strong>{caseVerdict(item)}</strong>
            <small>
              {item.analysis_publish_status ?? item.analysis_decision?.publish_status ?? item.state}
            </small>
            <span className="trading-recent-link">查看冻结依据 ↗</span>
          </button>
        ))
      ) : (
        <EmptyNote>
          {ledgerSentence({ failed: query.isError, pending: query.isPending, subject: "策略判定" })}
        </EmptyNote>
      )}
      {query.isError && query.data ? <EmptyNote>最近判定刷新失败，保留上次读取。</EmptyNote> : null}
      <div className="trading-recent-footer">
        <p>这些判断不代表旁边仓位的来源；执行记录按 Case 身份关联。</p>
        <ActionButton size="sm" onClick={onBrowse}>
          浏览策略判定
        </ActionButton>
      </div>
    </Card>
  );
}
