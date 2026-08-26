import { newsSymbolPath } from "@shared/routing/paths";
import { useRouteReferrer } from "@shared/routing/routeReferrer";
import { Card } from "@shared/ui/Card";
import { Link } from "react-router-dom";

import type { TradingCase, TradingCounts, TradingFloors } from "../api/tradingQueries";
import { CASE_STATE_ZH, REGIME_ZH, STRATEGY_ZH, TRIGGER_KIND_ZH } from "../model/tradingLabels";

import { TradingEmptyNote, TradingSourceLine } from "./TradingChrome";

/**
 * Where today's cases went, and — for the ones that stopped — the rule they stopped on.
 *
 * The primary funnel is the ledger's UTC budget day. Named rejections and the rolling 24 h research
 * evidence remain available in the collapsed technical disclosure below the approved workbench.
 *
 * The floors are shown beside them because a rejection reason means nothing without the number it failed.
 * They are the *capital* lane's thresholds and never the News gates — two sets, always side by side, neither
 * impersonating the other (#207 §4).
 */
export function TradingFunnel({
  counts,
  dayKey,
  ordersToday,
}: {
  counts: TradingCounts;
  dayKey: string;
  ordersToday: number;
}) {
  const byState = counts.cases_today_by_state ?? {};
  const total = Object.values(byState).reduce((sum, value) => sum + value, 0);
  const bars: Array<[string, number]> = [
    ["成案", total],
    ["政策放行", counts.policy_allowed_today ?? 0],
    ["提交订单", ordersToday],
    ["已了结", counts.closed_orders_today ?? 0],
    ["在场", counts.active_orders ?? 0],
  ];
  const max = Math.max(1, ...bars.map(([, value]) => value));
  const title =
    dayKey === new Date().toISOString().slice(0, 10) ? "今日案例去向" : `${dayKey} 案例去向`;

  return (
    <Card flush hint="混合窗口 · 各指标按自身账本时钟" title={title}>
      <div className="trading-funnel">
        {bars.map(([label, value]) => (
          <div className="trading-funnel-row" key={label}>
            <small>{label}</small>
            <span className="trading-funnel-track">
              <span style={{ width: `${Math.round((value / max) * 100)}%` }} />
            </span>
            <b>{value}</b>
          </div>
        ))}
        <p className="trading-funnel-note">
          oi_only 与 news_only 永不 live；只有对齐的 news_oi 才有资格走 live_reviewed。
        </p>
      </div>
    </Card>
  );
}

/** Long-form evidence remains available without displacing the approved operator workbench. */
export function TradingEvidence({
  cases,
  counts,
  floors,
}: {
  cases: readonly TradingCase[];
  counts: TradingCounts;
  floors: TradingFloors;
}) {
  const referrer = useRouteReferrer();
  return (
    <details className="trading-evidence">
      <summary>技术证据 · 影子研究、交易地板与未成单案例</summary>
      <div className="trading-evidence-body">
        <div className="trading-floors">
          <small>清算影子队列</small>
          <span>
            {Object.entries(counts.shadow_by_strategy ?? {}).length === 0
              ? "过去 24 小时暂无评估"
              : Object.entries(counts.shadow_by_strategy ?? {})
                  .map(([strategy, value]) => {
                    const cohort = counts.shadow_cohorts?.[strategy];
                    const result =
                      cohort?.mean_return_bps == null
                        ? ""
                        : ` · 1h 均值 ${(cohort.mean_return_bps / 100).toFixed(2)}%`;
                    return `${STRATEGY_ZH[strategy] ?? strategy} ${value}（完成 ${cohort?.completed ?? 0}）${result}`;
                  })
                  .join(" · ")}
            {counts.shadow_by_rule?.source_contract_incomplete
              ? ` · 来源契约不完整 ${counts.shadow_by_rule.source_contract_incomplete}`
              : ""}
            {!counts.liquidation_promotion_ready
              ? ` · 不可晋级：${counts.liquidation_promotion_reason || "证据不足"}`
              : ""}
          </span>
        </div>

        <div className="trading-floors">
          <small>清算事件研究</small>
          <span>
            {(counts.event_study_cohorts ?? []).length === 0
              ? "注册后的真实 holdout 尚未形成完成样本"
              : (counts.event_study_cohorts ?? [])
                  .map((cohort) => {
                    const horizons = ["5m", "15m", "1h"]
                      .map((label) => {
                        const measured = cohort.horizons?.[label];
                        const interval = measured?.bootstrap;
                        return interval == null
                          ? `${label} 未到期/缺失`
                          : `${label} ${(interval.mean_bps / 100).toFixed(2)}% [${(
                              interval.lower_95_bps / 100
                            ).toFixed(2)}%, ${(interval.upper_95_bps / 100).toFixed(2)}%]`;
                      })
                      .join(" · ");
                    const missing = Object.entries(cohort.missing_data ?? {})
                      .filter(([, value]) => value > 0)
                      .map(([reason, value]) => `${reason} ${value}`)
                      .join("、");
                    const exits = Object.entries(cohort.exit_by_reason ?? {})
                      .filter(([, value]) => value > 0)
                      .map(([reason, value]) => `${reason} ${value}`)
                      .join("、");
                    const net = cohort.net_ex_funding_bootstrap;
                    const promotion = (cohort.promotion_reasons ?? []).join("、");
                    return `${STRATEGY_ZH[cohort.strategy_id] ?? cohort.strategy_id} · ${cohort.venue}/${
                      cohort.liquidity_bucket
                    } · holdout ${cohort.holdout}/${cohort.evaluated} · 覆盖 ${(
                      cohort.coverage_bps / 100
                    ).toFixed(0)}% · 延迟均值 ${
                      cohort.mean_source_latency_ms == null
                        ? "—"
                        : `${cohort.mean_source_latency_ms}ms`
                    } · ${horizons} · MFE/MAE ${
                      cohort.mfe_mean_bps == null ? "—" : (cohort.mfe_mean_bps / 100).toFixed(2)
                    }%/${cohort.mae_mean_bps == null ? "—" : (cohort.mae_mean_bps / 100).toFixed(2)}%${
                      exits ? ` · 出场：${exits}` : ""
                    } · 净值(不含资金费) ${
                      net == null
                        ? "—"
                        : `${(net.mean_bps / 100).toFixed(2)}% [${(net.lower_95_bps / 100).toFixed(
                            2,
                          )}%, ${(net.upper_95_bps / 100).toFixed(2)}%]`
                    }${
                      missing ? ` · 缺失：${missing}` : ""
                    }${promotion ? ` · 晋级阻断：${promotion}` : ""}`;
                  })
                  .join(" ｜ ")}
          </span>
        </div>

        <div className="trading-floors">
          <small>交易地板</small>
          <span>
            鲸鱼盈利 ≥ {(floors.min_whale_long_profit_bps / 100).toFixed(0)}% · 持仓 ≥{" "}
            {floors.min_oi_value_usd} · 帧前 {floors.min_price_move_bps}–{floors.max_price_move_bps}{" "}
            bps 带内
          </span>
        </div>

        {cases.length === 0 ? (
          <TradingEmptyNote>过去 24 小时没有停在判定之前的案例。</TradingEmptyNote>
        ) : (
          <div className="trading-table">
            <div aria-hidden className="trading-case-head">
              <span>标的</span>
              <span>案例</span>
              <span>状态</span>
              <span>停在哪条规则</span>
            </div>
            {cases.map((row) => (
              <article className="trading-case-row" key={row.case_id}>
                <span className="trading-symbol">
                  <Link state={referrer} to={newsSymbolPath(row.base_symbol)}>
                    {row.base_symbol}
                  </Link>
                </span>
                <span className="trading-kind">
                  {STRATEGY_ZH[row.strategy_id] ?? row.strategy_id}
                  <small>{TRIGGER_KIND_ZH[row.trigger_kind] ?? row.trigger_kind}</small>
                </span>
                <span className="trading-state" data-state={row.state}>
                  {CASE_STATE_ZH[row.state] ?? row.state}
                </span>
                <span className="trading-note">
                  {/* The rule key verbatim: it is what an operator greps for, and it has no Chinese synonym. */}
                  <code>{row.policy_reason ?? row.policy_decision ?? "—"}</code>
                  {row.regime ? <small>{REGIME_ZH[row.regime] ?? row.regime}</small> : null}
                </span>
              </article>
            ))}
          </div>
        )}

        <TradingSourceLine path="GET /api/trading/status → counts · floors · GET /api/trading/orders → cases_without_orders" />
      </div>
    </details>
  );
}
