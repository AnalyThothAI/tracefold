import { ExternalLink } from "lucide-react";
import { type ReactNode, useState } from "react";

import {
  assetDirectionLabel,
  formatNumber,
  formatInstant,
  formatSigned,
  gapAxisLabel,
  horizonLabel,
  liveDeltaLabel,
  moduleLabel,
  momentumLabel,
  operatorLabel,
  outcomeStatusLabel,
} from "../model/macroPresentation";
import type {
  MacroAssetPresentation,
  MacroLiveDeltaReadData,
  MacroOutcomeReplayReadData,
  MacroReason,
  MacroThesisDetailReadData,
  MacroThesisV1,
} from "../model/macroTypes";

import { CitationItem, ConditionList } from "./MacroResearchEvidence";

type StatusTone = "positive" | "caution" | "negative" | "neutral";

function LazyDetails({
  children,
  className,
  summary,
}: {
  children: ReactNode;
  className?: string;
  summary: ReactNode;
}) {
  const [hasOpened, setHasOpened] = useState(false);
  return (
    <details
      className={className}
      onToggle={(event) => {
        if (event.currentTarget.open) setHasOpened(true);
      }}
    >
      <summary>{summary}</summary>
      {hasOpened ? children : null}
    </details>
  );
}

export function StatusGlyph({ tone }: { tone: StatusTone }) {
  return (
    <span aria-hidden="true" className="macro-research-status-glyph" data-tone={tone}>
      {tone === "positive" ? "✓" : tone === "negative" ? "!" : tone === "caution" ? "◆" : "•"}
    </span>
  );
}

export function AssetLedger({ assets }: { assets: MacroAssetPresentation[] }) {
  const groups = [
    ["actionable", "Actionable"],
    ["watch", "Watch"],
    ["evidence_gap", "Evidence gap"],
  ] as const;
  return (
    <section>
      <h3>十二资产条件附录</h3>
      <p>资产完整覆盖保留在档案中；方向是条件判断，不由事实动量推导。</p>
      <div className="macro-research-assets">
        {groups.map(([group, label]) => {
          const groupAssets = assets.filter((asset) => asset.group === group);
          if (!groupAssets.length) return null;
          return (
            <section data-group={group} key={group}>
              <h4>
                {label} <span>{groupAssets.length}</span>
              </h4>
              {groupAssets.map((asset) => {
                const oneWeek = asset.horizons[0];
                const oneMonth = asset.horizons[1];
                return (
                  <LazyDetails
                    key={asset.symbol}
                    summary={
                      <>
                        <strong>{asset.symbol}</strong>
                        <span>
                          <StatusGlyph tone={assetDirectionTone(oneWeek.outlook_direction)} />
                          1W {assetDirectionLabel(oneWeek.outlook_direction)} ·{" "}
                          <StatusGlyph tone={assetDirectionTone(oneMonth.outlook_direction)} />
                          1M {assetDirectionLabel(oneMonth.outlook_direction)}
                        </span>
                      </>
                    }
                  >
                    <dl>
                      <div>
                        <dt>1W 事实动量</dt>
                        <dd>
                          {momentumLabel(oneWeek.momentum_state)} ·{" "}
                          {formatSigned(oneWeek.momentum_value ?? null)}
                        </dd>
                      </div>
                      <div>
                        <dt>1W 条件展望</dt>
                        <dd>{oneWeek.reader_rationale.text}</dd>
                      </div>
                      <div>
                        <dt>1M 事实动量</dt>
                        <dd>
                          {momentumLabel(oneMonth.momentum_state)} ·{" "}
                          {formatSigned(oneMonth.momentum_value ?? null)}
                        </dd>
                      </div>
                      <div>
                        <dt>1M 条件展望</dt>
                        <dd>{oneMonth.reader_rationale.text}</dd>
                      </div>
                    </dl>
                    {oneWeek.reason ? <ReasonDetails reason={oneWeek.reason} /> : null}
                    {oneMonth.reason ? <ReasonDetails reason={oneMonth.reason} /> : null}
                  </LazyDetails>
                );
              })}
            </section>
          );
        })}
      </div>
    </section>
  );
}

function ReasonDetails({ reason }: { reason: MacroReason }) {
  return (
    <div className="macro-research-reason" data-impact={reason.impact}>
      <strong>
        <StatusGlyph tone={reason.impact === "blocked" ? "negative" : "caution"} />
        {reason.message}
      </strong>
      <span>
        影响：
        {
          { blocked: "阻断当前判断", limited: "限制判断范围", none: "不影响已发布判断" }[
            reason.impact
          ]
        }
      </span>
      {reason.next_action ? <span>恢复动作：{reason.next_action}</span> : null}
      {reason.next_check_at_ms ? (
        <span>下次检查：{formatInstant(reason.next_check_at_ms)}</span>
      ) : null}
    </div>
  );
}

export function LiveDeltaLedger({
  liveDelta,
  thesis,
}: {
  liveDelta: MacroLiveDeltaReadData | null;
  thesis: MacroThesisV1;
}) {
  const scopes = (liveDelta?.scopes ?? []).filter(
    (scope) => scope.items.length || scope.matched_binding_ids.length,
  );
  return (
    <section>
      <h3>当前观察层</h3>
      {liveDelta ? (
        <>
          <p>
            <StatusGlyph tone={deltaTone(liveDelta.mainline_validity)} />
            主线当前为“{liveDeltaLabel(liveDelta.mainline_validity)}”；命中论点{" "}
            {liveDelta.matched_claim_ids.length} 项、失效条件{" "}
            {liveDelta.matched_falsifier_ids.length} 项、检查点{" "}
            {liveDelta.matched_checkpoint_ids.length} 项。
          </p>
          <small>评估于 {formatInstant(liveDelta.evaluated_at_ms)}</small>
          {scopes.length ? (
            <ul className="macro-research-delta-scopes">
              {scopes.map((scope) => (
                <li key={`${scope.scope}:${scope.scope_id}`}>
                  <header>
                    <strong>{scope.label}</strong>
                    <span>
                      <StatusGlyph tone={deltaTone(scope.status)} />
                      {liveDeltaLabel(scope.status)}
                    </span>
                  </header>
                  {scope.items.map((item) => (
                    <article key={`${item.binding_type}:${item.condition_id}`}>
                      <strong>
                        {item.dataset_label} · {metricLabel(item.metric_name)}
                      </strong>
                      <span>
                        {item.observed_value == null
                          ? "当前值不可用"
                          : `观察值 ${formatNumber(item.observed_value)}${unitLabel(item.unit)}`}
                        ；阈值 {operatorLabel(item.operator)} {formatNumber(item.threshold)}
                        {unitLabel(item.unit)}
                      </span>
                      <small>{item.rationale}</small>
                      <small>
                        {item.observed_at_ms
                          ? `事实时间 ${formatInstant(item.observed_at_ms)}`
                          : `证据截点 ${formatInstant(item.observation_cutoff_ms)}`}
                      </small>
                      <details>
                        <summary>查看证据身份</summary>
                        <small>
                          Dataset {item.dataset_id} · metric {item.metric_name}
                        </small>
                      </details>
                      <ReasonDetails reason={item.reason} />
                    </article>
                  ))}
                </li>
              ))}
            </ul>
          ) : null}
        </>
      ) : (
        <p>尚未形成可用于本 publication 的 Live Delta。</p>
      )}
      <div className="macro-research-condition-grid">
        <ConditionList rows={thesis.mainline.falsifiers} title="主线失效条件" />
        <ConditionList rows={thesis.mainline.checkpoints} title="下一检查点" />
      </div>
    </section>
  );
}

export function OutcomeLedger({
  outcomeReplay,
}: {
  outcomeReplay: MacroOutcomeReplayReadData | null;
}) {
  return (
    <section>
      <h3>Outcome Replay</h3>
      <ul className="macro-research-outcomes">
        {outcomeReplay?.horizons.map((horizon) => (
          <li key={horizon.horizon}>
            <div>
              <strong>{horizonLabel(horizon.horizon)}</strong>
              <span>
                <StatusGlyph tone={outcomeTone(horizon.status)} />
                {horizon.benchmark_symbol} · {outcomeStatusLabel(horizon.status)}
              </span>
              <span>
                {horizon.realized_return_pct == null
                  ? `到期 ${formatInstant(horizon.expires_at_ms)}`
                  : `实现收益 ${formatSigned(horizon.realized_return_pct)}%`}
              </span>
              <ReasonDetails reason={horizon.reason} />
              <SourceReasonAudit code={horizon.source_reason_code} />
            </div>
            {horizon.asset_results.length ? (
              <LazyDetails summary={`资产结果（${horizon.asset_results.length}）`}>
                <ul>
                  {horizon.asset_results.map((asset) => (
                    <li key={`${asset.symbol}:${asset.horizon}`}>
                      <strong>{asset.symbol}</strong>
                      <span>
                        <StatusGlyph tone={outcomeTone(asset.status)} />
                        发布方向 {assetDirectionLabel(asset.published_direction)} ·{" "}
                        {outcomeStatusLabel(asset.status)}
                      </span>
                      <span>
                        {asset.realized_return_pct == null
                          ? `到期 ${formatInstant(asset.expires_at_ms)}`
                          : `实现收益 ${formatSigned(asset.realized_return_pct)}%`}
                      </span>
                      <ReasonDetails reason={asset.reason} />
                      <SourceReasonAudit code={asset.source_reason_code} />
                    </li>
                  ))}
                </ul>
              </LazyDetails>
            ) : null}
          </li>
        )) ?? <li>尚未创建结果复盘。</li>}
      </ul>
    </section>
  );
}

function SourceReasonAudit({ code }: { code: string }) {
  return (
    <details>
      <summary>查看来源状态审计</summary>
      <code>{code}</code>
    </details>
  );
}

function metricLabel(value: string): string {
  return (
    {
      change_1d_bp: "1日变化",
      change_1m_bp: "1月变化",
      change_1w_bp: "1周变化",
      change_3m_bp: "3个月变化",
      change_4w_bp: "4周变化",
      change_4w_pct: "4周变化",
      change_wow_bp: "周环比",
      change_wow_pct: "周环比",
      mom_bp: "月环比",
      mom_pct: "月环比",
      qoq_annualized_pct: "季环比年化",
      qoq_bp: "季环比",
      return_1d_pct: "1日回报",
      return_1m_pct: "1月回报",
      return_1w_pct: "1周回报",
      revision: "前值修订",
      surprise: "相对预期",
      three_month_annualized_pct: "3个月年化",
      yoy_bp: "同比",
      yoy_pct: "同比",
    }[value] ?? "登记指标"
  );
}

function unitLabel(value: string | null): string {
  if (!value) return "";
  return (
    {
      basis_points: "bp",
      billions_chained_2017_usd: "十亿 2017 年不变价美元",
      billions_usd: "十亿美元",
      bp: "bp",
      index: "点",
      index_points: "点",
      millions_usd: "百万美元",
      percent: "%",
      percent_open_interest: "% OI",
      persons: "人",
      price: "",
      thousands_persons: "千人",
      usd_per_barrel: "美元/桶",
      usdt: " USDT",
    }[value] ?? "（单位未解释）"
  );
}

export function CitationLedger({ thesis }: { thesis: MacroThesisV1 }) {
  return (
    <section className="macro-research-citation-ledger">
      <header>
        <h3>证据引用</h3>
        <span>{thesis.citations.length} 条闭合引用</span>
      </header>
      <ol>
        {thesis.citations.map((citation) => (
          <CitationItem citation={citation} key={citation.evidence_ref} showAuditIdentity />
        ))}
      </ol>
    </section>
  );
}

export function PublicationAuditAppendix({
  appendix,
}: {
  appendix: NonNullable<MacroThesisDetailReadData["appendix"]>;
}) {
  return (
    <section className="macro-research-publication-audit">
      <header>
        <h3>发布时数据质量</h3>
        <span>
          Evidence Pack 冻结于 {formatInstant(appendix.sealed_at_ms)} · 最晚来源接收{" "}
          {formatInstant(appendix.source_max_received_at_ms)}
        </span>
      </header>
      <dl className="macro-research-quality-summary">
        <QualityState
          label="覆盖"
          state={appendix.data_quality.coverage_state}
          text={qualityStateLabel(appendix.data_quality.coverage_state)}
        />
        <QualityState
          label="当前健康"
          state={appendix.data_quality.current_health_state}
          text={qualityStateLabel(appendix.data_quality.current_health_state)}
        />
        <QualityState
          label="历史深度"
          state={appendix.data_quality.history_depth_state}
          text={qualityStateLabel(appendix.data_quality.history_depth_state)}
        />
      </dl>
      <ul className="macro-research-quality-modules">
        {appendix.data_quality.modules.map((module) => (
          <li key={module.module_id}>
            <strong>
              <StatusGlyph tone={qualityTone(module.current_health_state)} />
              {module.label}
            </strong>
            <span>
              覆盖 {qualityStateLabel(module.coverage_state)} · 当前健康{" "}
              {qualityStateLabel(module.current_health_state)} · 历史{" "}
              {qualityStateLabel(module.history_depth_state)}
            </span>
            <small>
              可用能力 {module.available_capabilities}/{module.expected_capabilities} · 当前数据{" "}
              {module.current_datasets}/{module.tracked_datasets} · 历史完成{" "}
              {module.complete_history_datasets}/{module.tracked_history_datasets}
            </small>
            {module.backfill_state ? (
              <small>
                历史回填：{backfillStateLabel(module.backfill_state)}
                {module.backfill_worker_enabled === false ? "（worker 未启用）" : ""}
              </small>
            ) : null}
            {module.reasons.length ? (
              <ul>
                {module.reasons.map((reason) => (
                  <li key={`${reason.code}:${reason.message}`}>{reason.message}</li>
                ))}
              </ul>
            ) : null}
          </li>
        ))}
      </ul>

      <LazyDetails
        className="macro-research-receipts"
        summary={`Reconciliation receipts（${appendix.reconciliation_receipts.length}）`}
      >
        {appendix.reconciliation_receipts.length ? (
          appendix.reconciliation_receipts.map((receipt) => (
            <section key={`${receipt.module_id}:${receipt.concept_id}`}>
              <h4>
                <StatusGlyph tone={receiptTone(receipt.state)} />
                {receipt.module_label} · {receiptStateLabel(receipt.state)}
              </h4>
              <p>对账概念：{receipt.concept_id}</p>
              <small>
                选择规则：{selectionPolicyLabel(receipt.selection_policy)} · 身份规则：
                {identityPolicyLabel(receipt.identity_policy)}
              </small>
              <div
                aria-label={`${receipt.module_label} 对账观测表`}
                className="macro-research-audit-table"
                role="region"
              >
                <table>
                  <caption>{receipt.module_label} 对账来源</caption>
                  <thead>
                    <tr>
                      <th scope="col">Dataset</th>
                      <th scope="col">来源角色</th>
                      <th scope="col">参考期</th>
                      <th scope="col">值</th>
                      <th scope="col">单位</th>
                    </tr>
                  </thead>
                  <tbody>
                    {receipt.observations.map((observation) => (
                      <tr key={`${observation.dataset_id}:${observation.source_role}`}>
                        <td>{observation.dataset_id}</td>
                        <td>{auditSourceRoleLabel(observation.source_role)}</td>
                        <td>{observation.reference ?? "未提供"}</td>
                        <td>{auditValue(observation.value)}</td>
                        <td>{unitLabel(observation.unit)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {receipt.comparisons.length ? (
                <ul>
                  {receipt.comparisons.map((comparison) => (
                    <li
                      key={`${comparison.left_dataset_id}:${comparison.right_dataset_id}:${comparison.status}`}
                    >
                      {reconciliationStatusLabel(comparison.status)}：差异{" "}
                      {formatSigned(comparison.difference)}
                      {unitLabel(comparison.unit)}，容差 {formatNumber(comparison.tolerance)}
                      {unitLabel(comparison.unit)}
                    </li>
                  ))}
                </ul>
              ) : (
                <p>没有同参考期、同单位的可比较来源。</p>
              )}
            </section>
          ))
        ) : (
          <p>本 Evidence Pack 没有需要多来源对账的概念。</p>
        )}
      </LazyDetails>

      <LazyDetails
        className="macro-research-lineage"
        summary={`完整来源谱系（${appendix.source_lineage.length}）`}
      >
        <p>
          以下事实身份、来源时钟与链接冻结于 {appendix.session_date} 的 publication{" "}
          {appendix.publication_id}，不代表当前来源状态。
        </p>
        <ol aria-label="Evidence Pack 发布时来源谱系" className="macro-research-lineage-list">
          {appendix.source_lineage.map((source) => (
            <li key={`${source.module_id}:${source.dataset_id}`}>
              <header>
                <div>
                  <strong>{source.label}</strong>
                  <span>
                    {source.module_label} ·{" "}
                    {source.source_role
                      ? auditSourceRoleLabel(source.source_role)
                      : "来源角色未登记"}
                  </span>
                </div>
                <span>
                  <StatusGlyph tone={qualityTone(source.current_health ?? "unavailable")} />
                  {source.current_health
                    ? qualityStateLabel(source.current_health)
                    : "发布时状态未登记"}
                </span>
              </header>
              <dl>
                <div>
                  <dt>参考期</dt>
                  <dd>{source.reference ?? "未提供"}</dd>
                </div>
                <div>
                  <dt>发布值</dt>
                  <dd>
                    {auditValue(source.value)}
                    {source.unit ? unitLabel(source.unit) : ""}
                  </dd>
                </div>
                <div>
                  <dt>事实观测</dt>
                  <dd>
                    {source.observed_at_ms == null
                      ? "未提供"
                      : formatInstant(source.observed_at_ms)}
                  </dd>
                </div>
                <div>
                  <dt>来源发布</dt>
                  <dd>
                    {source.published_at_ms == null
                      ? "未提供"
                      : formatInstant(source.published_at_ms)}
                  </dd>
                </div>
                <div>
                  <dt>系统接收</dt>
                  <dd>
                    {source.received_at_ms == null
                      ? "未提供"
                      : formatInstant(source.received_at_ms)}
                  </dd>
                </div>
              </dl>
              <footer>
                {source.source_url ? (
                  <a href={source.source_url} rel="noreferrer" target="_blank">
                    原始来源 <ExternalLink aria-hidden="true" />
                  </a>
                ) : (
                  <span>未提供原始来源链接</span>
                )}
                <details>
                  <summary>查看来源技术身份</summary>
                  <code>Dataset {source.dataset_id}</code>
                </details>
              </footer>
            </li>
          ))}
        </ol>
      </LazyDetails>
    </section>
  );
}

export function GapLedger({ thesis }: { thesis: MacroThesisV1 }) {
  return (
    <section className="macro-research-gaps">
      <header>
        <h3>真实证据缺口</h3>
      </header>
      <ul>
        {thesis.gaps.map((gap) => (
          <li key={gap.gap_id}>
            <strong>
              {moduleLabel(gap.module_id)} · {gapAxisLabel(gap.axis)}
            </strong>
            <p>{gap.reason}</p>
            {gap.affected_claim_ids.length ? (
              <small>影响 {gap.affected_claim_ids.length} 个已发布论点</small>
            ) : null}
          </li>
        ))}
      </ul>
    </section>
  );
}

function deltaTone(value: MacroLiveDeltaReadData["mainline_validity"]): StatusTone {
  if (value === "confirming") return "positive";
  if (value === "invalidation_triggered") return "negative";
  if (value === "weakening" || value === "insufficient") return "caution";
  return "neutral";
}

function assetDirectionTone(
  value: MacroAssetPresentation["horizons"][number]["outlook_direction"],
): StatusTone {
  if (value === "bullish") return "positive";
  if (value === "bearish") return "negative";
  if (value === "no_call") return "caution";
  return "neutral";
}

function outcomeTone(value: MacroOutcomeReplayReadData["horizons"][number]["status"]): StatusTone {
  if (value === "evaluated") return "positive";
  if (value === "insufficient") return "negative";
  return "caution";
}

function qualityTone(value: string): StatusTone {
  if (value === "complete" || value === "current" || value === "not_required") return "positive";
  if (value === "unavailable" || value === "insufficient" || value === "failed") return "negative";
  return "caution";
}

function receiptTone(value: string): StatusTone {
  if (value === "complete") return "positive";
  if (value === "insufficient") return "negative";
  return "caution";
}

function QualityState({ label, state, text }: { label: string; state: string; text: string }) {
  return (
    <div>
      <dt>{label}</dt>
      <dd>
        <StatusGlyph tone={qualityTone(state)} />
        {text}
      </dd>
    </div>
  );
}

function qualityStateLabel(value: string): string {
  return (
    {
      complete: "完整",
      current: "当前可用",
      degraded: "局部降级",
      insufficient: "证据不足",
      not_required: "无需历史",
      partial: "部分覆盖",
      unavailable: "不可用",
    }[value] ?? "状态未解释"
  );
}

function backfillStateLabel(value: string): string {
  return (
    {
      complete: "已完成",
      failed: "失败",
      not_required: "无需回填",
      paused: "已暂停",
      queued: "等待执行",
      retry_wait: "等待重试",
      running: "执行中",
    }[value] ?? "状态未解释"
  );
}

function receiptStateLabel(value: string): string {
  return (
    {
      complete: "来源齐全",
      insufficient: "没有可用来源",
      partial: "部分来源缺失",
    }[value] ?? "状态未解释"
  );
}

function reconciliationStatusLabel(value: string): string {
  return (
    {
      divergent: "超出容差",
      reference_mismatch: "参考期不一致",
      within_tolerance: "容差内一致",
    }[value] ?? "状态未解释"
  );
}

function selectionPolicyLabel(value: string): string {
  return value === "decision_primary_only_no_fallback"
    ? "只使用决策主源，不以代理回填"
    : "规则详见审计合同";
}

function identityPolicyLabel(value: string): string {
  return (
    {
      same_concept_same_reference_only: "只比较同一概念、同一参考期的来源",
      separate_source_facts_no_blend: "各来源事实保持独立，不混合",
    }[value] ?? "规则详见审计合同"
  );
}

function auditSourceRoleLabel(value: string): string {
  return (
    {
      decision_primary: "决策主源",
      derived: "派生事实",
      history: "历史序列",
      intraday_proxy: "盘中代理",
      official_document: "官方文件",
      official_cross_check: "官方交叉核验",
      reconciliation_only: "仅用于对账",
      release: "官方发布",
    }[value] ?? "其他登记来源"
  );
}

function auditValue(value: string | number | null): string {
  if (value == null) return "未提供";
  return typeof value === "number" ? formatNumber(value) : value;
}
