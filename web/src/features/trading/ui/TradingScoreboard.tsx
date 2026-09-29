import { Card } from "@shared/ui/Card";
import { EmptyNote } from "@shared/ui/EmptyNote";
import * as PageState from "@shared/ui/PageState";

import { useTradingScoreboardWithToken } from "../api/tradingQueries";

const columns = ["来源", "选中", "评估成功", "发布", "执行受理", "成交"] as const;
const keys = [
  "triggers",
  "selected",
  "assessed",
  "published",
  "execution_accepted",
  "filled",
] as const;

export function TradingScoreboard({ token }: { token: string }) {
  const query = useTradingScoreboardWithToken(token);
  const data = query.data;
  if (query.isError && !data)
    return <PageState.Error error={query.error} onRetry={() => void query.refetch()} />;
  if (!data) return <PageState.Loading label="正在读取 LIVE 纸面记分板" layout="panel" rows={4} />;
  return (
    <section aria-label="策略记分板" className="trading-scoreboard">
      <Card
        flush
        title="预测 → 决策 → 纸面 → 执行"
        hint="同一批 LIVE Case；纸面成本含双边 taker 与半点差"
      >
        <div className="trading-scoreboard-funnel">
          {keys.map((key, index) => (
            <div key={key}>
              <span>{columns[index]}</span>
              <strong>{data.funnel[key] ?? 0}</strong>
            </div>
          ))}
        </div>
      </Card>
      {data.programs.length ? (
        data.programs.map((program) => (
          <Card
            key={`${program.program_sha}:${program.route}`}
            flush
            title={`程序 ${program.program_sha.slice(0, 12)}`}
            hint={`路由 ${program.route || "未记录"} · 评估 ${program.assessments}`}
          >
            {Object.entries(program.failures).length ? (
              <p>
                具名失败：
                {Object.entries(program.failures)
                  .map(([name, count]) => `${name} ${count}`)
                  .join(" · ")}
              </p>
            ) : null}
            <div className="trading-scoreboard-table-wrap">
              <table className="trading-scoreboard-table">
                <thead>
                  <tr>
                    <th>Policy</th>
                    <th>覆盖率</th>
                    <th>纸面腿</th>
                    <th>平均 R</th>
                    <th>95% 区间</th>
                    <th>胜率</th>
                  </tr>
                </thead>
                <tbody>
                  {program.policies.map((policy) => (
                    <tr key={`${policy.policy_id}:${policy.policy_version}`}>
                      <th>{policy.policy_id}</th>
                      <td>{(Number(policy.coverage) * 100).toFixed(1)}%</td>
                      <td>
                        {policy.scored}/{policy.actions}
                      </td>
                      <td>{policy.average_r ?? "数据不足"}</td>
                      <td>
                        {policy.ci_low == null
                          ? "数据不足"
                          : `${policy.ci_low} ~ ${policy.ci_high}`}
                      </td>
                      <td>
                        {policy.win_rate == null
                          ? "数据不足"
                          : `${(Number(policy.win_rate) * 100).toFixed(1)}%`}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <p>
              预测：Brier {program.forecast.multiclass_brier ?? "数据不足"} · log loss{" "}
              {program.forecast.log_loss ?? "数据不足"} · BSS{" "}
              {program.forecast.brier_skill_score ?? "数据不足"} · {program.forecast.legs} 条腿
            </p>
            {program.forecast.reliability?.length ? (
              <div className="trading-scoreboard-table-wrap">
                <table className="trading-scoreboard-table">
                  <caption>止盈概率可靠性</caption>
                  <thead>
                    <tr>
                      <th>预测概率</th>
                      <th>纸面腿</th>
                      <th>实际止盈率</th>
                    </tr>
                  </thead>
                  <tbody>
                    {program.forecast.reliability.map((bin) => (
                      <tr key={bin.bin}>
                        <th>
                          {bin.bin * 10}–{(bin.bin + 1) * 10}%
                        </th>
                        <td>{bin.count}</td>
                        <td>{(Number(bin.observed_tp_rate) * 100).toFixed(1)}%</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : null}
            <p>
              执行与纸面 R 偏差：{String(program.execution_deviation.average_r_delta ?? "数据不足")}{" "}
              · {String(program.execution_deviation.scored ?? 0)} 笔
            </p>
          </Card>
        ))
      ) : (
        <EmptyNote>当前窗口还没有可比较的程序结果。</EmptyNote>
      )}
    </section>
  );
}
