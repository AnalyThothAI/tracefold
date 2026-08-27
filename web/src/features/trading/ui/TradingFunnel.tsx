import { Card } from "@shared/ui/Card";

import type { TradingCounts, TradingFloors } from "../api/tradingQueries";
import { gateReasonLabel, STRATEGY_ZH } from "../model/tradingLabels";

import { TradingEmptyNote, TradingSourceLine } from "./TradingChrome";

/** How many source frames the admission ledger holds for a window, whatever their answer. */
function sourcesSeen(counts: Record<string, number> | undefined): number {
  return Object.values(counts ?? {}).reduce((sum, value) => sum + value, 0);
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
    <Card flush hint="24 小时 · 按帧观测时刻，跨 UTC 日仍可查" title="未成案的来源帧">
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
  counts,
  floors,
}: {
  counts: TradingCounts;
  floors: TradingFloors;
}) {
  return (
    <details className="trading-evidence">
      <summary>技术证据 · 影子研究与车道地板</summary>
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

        <TradingSourceLine path="GET /api/trading/status → counts · floors" />
      </div>
    </details>
  );
}
