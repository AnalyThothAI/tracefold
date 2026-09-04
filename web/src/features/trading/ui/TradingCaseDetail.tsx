import { Card } from "@shared/ui/Card";

import type { TradingCase } from "../api/tradingQueries";
import { caseChecks, caseVerdict } from "../model/tradingCases";
import { bpsPercent, caseClock, policyLabel } from "../model/tradingLabels";

/**
 * One Case in full: its terminal answer, and every condition the policy executed to reach it.
 *
 * Every threshold on screen is the Case's own. That is the whole point of the panel — a console holding
 * only today's configuration cannot explain a Case frozen a week ago, and the version that tried printed
 * conflicts on rows that had passed.
 *
 * It lived on `/news/alpha` until #460, which removed that page. The row list above it was a second view
 * of `GET /api/trading/cases`; this was not, and deleting it would have made the frozen per-check
 * evidence a database query rather than something an operator can read.
 */
export function TradingCaseDetail({ item }: { item: TradingCase }) {
  const checks = caseChecks(item);
  return (
    <section aria-label={`案例 ${item.base_symbol}`} className="trading-case-detail">
      <Card
        flush
        hint={`${policyLabel(item.policy_id)} · ${(item.policy_config_digest ?? "").slice(0, 12) || "—"}`}
        title={`${item.base_symbol} · ${caseVerdict(item)}`}
      >
        <dl className="trading-case-facts">
          <div className="trading-case-fact">
            <dt>案例</dt>
            <dd>
              <code>{item.case_id}</code>
            </dd>
          </div>
          <div className="trading-case-fact">
            <dt>触发来源</dt>
            <dd>
              <code>{item.event_id ?? "—"}</code>
            </dd>
          </div>
          <div className="trading-case-fact">
            <dt>冻结时刻</dt>
            <dd>{caseClock(item.observed_at_ms)}</dd>
          </div>
          <div className="trading-case-fact">
            <dt>判定时刻</dt>
            <dd>{caseClock(item.decided_at_ms)}</dd>
          </div>
          <div className="trading-case-fact">
            <dt>市场</dt>
            <dd>{item.market_key ?? "—"}</dd>
          </div>
          <div className="trading-case-fact">
            <dt>冻结标记价</dt>
            <dd>{item.mark_price ?? "—"}</dd>
          </div>
          <div className="trading-case-fact">
            <dt>前置涨跌</dt>
            <dd>{bpsPercent(item.pre_move_bps)}</dd>
          </div>
          <div className="trading-case-fact">
            <dt>Signal</dt>
            <dd>{item.state === "SIGNAL_EMITTED" ? "已发出，见下方 Signal 账本" : "未发出"}</dd>
          </div>
        </dl>
      </Card>

      <Card
        flush
        hint="这张表的阈值来自案例本身，不是当前配置"
        title={`冻结判定证据 · ${item.manifest_version ?? "—"}`}
      >
        {checks.length ? (
          <div className="trading-case-checks">
            <div aria-hidden className="trading-case-check">
              <span>条件</span>
              <span>比较</span>
              <span>阈值</span>
              <span>实测</span>
              <span>结果</span>
            </div>
            {checks.map((check, index) => (
              <div
                className="trading-case-check"
                data-passed={check.passed}
                key={`${check.check}-${index}`}
              >
                <span>{check.check}</span>
                <span>{check.operator}</span>
                <span>{check.threshold_label}</span>
                <span>{check.measured_label}</span>
                <span>{check.passed ? "通过" : "未通过"}</span>
              </div>
            ))}
          </div>
        ) : (
          <p className="trading-case-facts">
            这个案例在冻结逐条证据之前写入（#331 之前）。它的终局与规则仍然是{" "}
            <code>{item.policy_reason ?? "—"}</code>，冻结配置在下方。
          </p>
        )}
      </Card>

      <Card flush hint="案例冻结时执行的整组数字" title="冻结策略配置">
        <dl className="trading-case-facts">
          {Object.entries(item.policy_config ?? {}).map(([key, value]) => (
            <div className="trading-case-fact" key={key}>
              <dt>
                <code>{key}</code>
              </dt>
              <dd>{value}</dd>
            </div>
          ))}
          {Object.keys(item.policy_config ?? {}).length === 0 ? (
            <div className="trading-case-fact">
              <dt>冻结配置</dt>
              <dd>该案例未记录（#331 之前的清单版本）</dd>
            </div>
          ) : null}
        </dl>
      </Card>
    </section>
  );
}
