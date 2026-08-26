import { newsSymbolPath } from "@shared/routing/paths";
import { useRouteReferrer } from "@shared/routing/routeReferrer";
import { Card } from "@shared/ui/Card";
import { Link } from "react-router-dom";

import type { TradingCase, TradingCounts, TradingFloors } from "../api/tradingQueries";
import {
  CASE_STATE_ZH,
  gateReasonLabel,
  REGIME_ZH,
  STRATEGY_ZH,
  TRIGGER_KIND_ZH,
} from "../model/tradingLabels";

import { TradingEmptyNote, TradingSourceLine } from "./TradingChrome";

/** How many source frames the admission ledger holds for a window, whatever their answer. */
function sourcesSeen(counts: Record<string, number> | undefined): number {
  return Object.values(counts ?? {}).reduce((sum, value) => sum + value, 0);
}

/**
 * Where today's frames went, and — for the ones that stopped — the rule they stopped on.
 *
 * The funnel used to start at 成案, which made the whole population that never reached one invisible: a
 * lane at zero orders had nothing on screen distinguishing "the upstream is quiet" from "every frame was
 * below the liquidity floor" (#264). The first two rows are the durable admission ledger and are the only
 * ones that survive the UTC day roll; the three below them are the ledger's own budget-day counts. The
 * two clocks are labelled rather than merged, because a 24 h rolling count and a calendar day drawn as one
 * bar chart is two intervals impersonating one.
 *
 * The floors are shown beside the rejections because a reason means nothing without the number it failed.
 * They are the *capital* lane's thresholds and never the News gates — two sets, always side by side,
 * neither impersonating the other (#207 §4).
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
  const admitted = counts.candidate_counts_24h?.CASE_CREATED ?? 0;
  const seen = sourcesSeen(counts.candidate_counts_24h);
  const bars: Array<[string, number, string]> = [
    ["上游帧", seen, "24h"],
    ["过准入", admitted, "24h"],
    ["成案", total, "日"],
    ["政策放行", counts.policy_allowed_today ?? 0, "日"],
    ["提交订单", ordersToday, "日"],
    ["已了结", counts.closed_orders_today ?? 0, "日"],
    ["在场", counts.active_orders ?? 0, "当前"],
  ];
  const max = Math.max(1, ...bars.map(([, value]) => value));
  const title =
    dayKey === new Date().toISOString().slice(0, 10) ? "今日案例去向" : `${dayKey} 案例去向`;

  return (
    <Card flush hint="混合窗口 · 各指标按自身账本时钟" title={title}>
      <div className="trading-funnel">
        {bars.map(([label, value, clock]) => (
          <div className="trading-funnel-row" key={label}>
            <small>
              {label}
              <em>{clock}</em>
            </small>
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

/**
 * Why the frames that never reached a case were refused (#264).
 *
 * This is the answer that used to exist only in `trading_runtime_state.funnel` and was overwritten at
 * every UTC midnight. It is a *source admission* report and deliberately not merged with the case table
 * below it: a frame refused before a manifest could be frozen and a case a strategy declined are two
 * different stages, and one list holding both taught a reader that 成案 and 有交易 were the same thing.
 *
 * The reason key is rendered verbatim beside its Chinese for the same reason `policy_reason` is.
 */
export function TradingAdmission({ counts }: { counts: TradingCounts }) {
  const reasons = Object.entries(counts.candidate_reasons_24h ?? {})
    .filter(([key]) => key !== "freeze:case_created")
    .sort((left, right) => right[1] - left[1] || left[0].localeCompare(right[0]));
  const seen = sourcesSeen(counts.candidate_counts_24h);
  const week = sourcesSeen(counts.candidate_counts_7d);

  return (
    <Card
      flush
      hint="24 小时 · 按帧观测时刻，跨 UTC 日仍可查"
      title="未成案的来源帧"
    >
      <div className="trading-admission">
        {seen === 0 ? (
          <TradingEmptyNote>
            {week === 0
              ? "准入账本里还没有任何来源帧——上游没有推送，或资本通道尚未运行过。"
              : `过去 24 小时没有来源帧；7 天内有 ${week} 帧。`}
          </TradingEmptyNote>
        ) : (
          reasons.map(([key, value]) => (
            <div className="trading-admission-row" key={key}>
              <span>{gateReasonLabel(key)}</span>
              <code>{key}</code>
              <b>{value}</b>
            </div>
          ))
        )}
        <TradingSourceLine path="GET /api/trading/status → counts.candidate_reasons_24h（trading_candidate_gate_decisions）" />
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
