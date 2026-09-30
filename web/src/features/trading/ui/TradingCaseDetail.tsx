import { Card } from "@shared/ui/Card";
import { Link } from "react-router-dom";

import type { TradingCase } from "../api/tradingQueries";
import { caseClock } from "../model/tradingLabels";

export function TradingCaseDetail({ item }: { item: TradingCase }) {
  return (
    <section aria-label={`案例 ${item.asset_id}`} className="trading-case-detail">
      <Card
        flush
        title={`${item.asset_id} · ${item.state}`}
        hint={`${item.trigger_kind} · ${caseClock(item.decided_at_ms)}`}
      >
        <dl className="trading-case-facts">
          <div>
            <dt>Case</dt>
            <dd>
              <code>{item.case_id}</code>
            </dd>
          </div>
          <div>
            <dt>冻结输入</dt>
            <dd>
              <code>{item.view_sha256 ?? "尚未冻结"}</code>
            </dd>
          </div>
          <div>
            <dt>几何</dt>
            <dd>{item.geometry_version ?? "待计算"}</dd>
          </div>
          <div>
            <dt>失败原因</dt>
            <dd>{item.failure_code ?? "—"}</dd>
          </div>
        </dl>
        <Link to={`/trading?tab=executions&execution_case=${item.case_id}`}>查看关联执行</Link>
      </Card>
      <Card flush title="两侧预测" hint="程序版本与模型路由随评估保存">
        {item.assessments?.length ? (
          item.assessments.map((assessment) => (
            <div key={assessment.assessment_id}>
              <p>
                <code>{assessment.run_id.slice(0, 12)}</code> · {assessment.route} ·{" "}
                {assessment.status}
              </p>
              {assessment.forecast ? (
                <pre>{JSON.stringify(assessment.forecast, null, 2)}</pre>
              ) : null}
            </div>
          ))
        ) : (
          <p>暂无评估结果。</p>
        )}
      </Card>
      <Card flush title="同场 Policy 动作" hint="只有配置中的 live policy 可能发布 Signal">
        <ul>
          {item.policy_actions?.map((action) => (
            <li key={action.action_id}>
              {action.policy_id}: {action.action} · {action.reason} · {action.publish_status}
            </li>
          ))}
        </ul>
      </Card>
      <Card flush title="LIVE 纸面两腿" hint="决策后首根 1m 收盘锚定，止损同根优先">
        <ul>
          {item.paper_legs?.map((leg) => (
            <li key={`${leg.side}:${leg.geometry_version}`}>
              {leg.side}:{" "}
              {leg.status === "complete"
                ? `${leg.outcome} · ${leg.net_r}R`
                : `missing · ${leg.reason}`}
            </li>
          ))}
        </ul>
      </Card>
    </section>
  );
}
