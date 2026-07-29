import {
  assetDirectionLabel,
  changeStatusLabel,
  confidenceLabel,
  formatInstant,
  formatSigned,
  horizonLabel,
  liveDeltaLabel,
  moduleLabel,
  moduleRoleLabel,
  momentumLabel,
  operatorLabel,
  outcomeStatusLabel,
  stageLabel,
  stanceLabel,
} from "../model/macroPresentation";
import type {
  MacroLiveDeltaV2,
  MacroOutcomeReplayV2,
  MacroRecoveryItem,
  MacroThesisV2,
} from "../model/macroTypes";

import "./MacroThesisReport.css";

export function MacroThesisReport({
  thesis,
  liveDelta,
  outcomeReplay,
  recovery,
  compact = false,
}: {
  thesis: MacroThesisV2;
  liveDelta: MacroLiveDeltaV2 | null;
  outcomeReplay: MacroOutcomeReplayV2 | null;
  recovery: MacroRecoveryItem[];
  compact?: boolean;
}) {
  const citations = new Map(thesis.citations.map((item) => [item.evidence_ref, item]));
  const deltaItems = new Map((liveDelta?.items ?? []).map((item) => [item.condition_id, item]));
  return (
    <div className="macro-thesis-report" data-compact={compact || undefined}>
      <section className="macro-thesis-report__mainline">
        <div className="macro-thesis-report__eyebrow">
          <span>{stanceLabel(thesis.mainline.stance)}</span>
          <span>{stageLabel(thesis.mainline.stage)}</span>
          <span>{horizonLabel(thesis.mainline.horizon)}</span>
          <span>{confidenceLabel(thesis.mainline.confidence)}</span>
        </div>
        <h2>{thesis.mainline.title}</h2>
        <p>{thesis.mainline.thesis}</p>
        {thesis.mainline.no_call_reason ? <aside>{thesis.mainline.no_call_reason}</aside> : null}
        {thesis.mainline.causal_edges.length ? (
          <ol aria-label="主线因果链" className="macro-thesis-report__causal-chain">
            {thesis.mainline.causal_edges.map((edge) => (
              <li key={edge.edge_id}>
                <strong>{edge.source}</strong>
                <span>通过</span>
                <b>{edge.mechanism}</b>
                <span>作用于</span>
                <strong>{edge.target}</strong>
                <EvidenceRefs refs={edge.evidence_refs} citations={citations} />
              </li>
            ))}
          </ol>
        ) : null}
      </section>

      {thesis.alternative ? (
        <section className="macro-thesis-report__section">
          <header>
            <span>ALTERNATIVE</span>
            <h3>唯一备择解释：{thesis.alternative.title}</h3>
          </header>
          <p>{thesis.alternative.thesis}</p>
        </section>
      ) : null}

      {thesis.tensions.length ? (
        <section className="macro-thesis-report__section">
          <header>
            <span>UNRESOLVED TENSIONS</span>
            <h3>尚未闭合的反证</h3>
          </header>
          <div className="macro-thesis-report__grid">
            {thesis.tensions.map((tension) => (
              <article key={tension.tension_id}>
                <strong>{tension.statement}</strong>
                <p>{tension.side_a.statement}</p>
                <p>{tension.side_b.statement}</p>
                <small>{tension.unresolved_reason}</small>
              </article>
            ))}
          </div>
        </section>
      ) : null}

      <section className="macro-thesis-report__section">
        <header>
          <span>MATERIAL SCOPE</span>
          <h3>本次真正重要的模块</h3>
        </header>
        {thesis.module_assessments.length ? (
          <div className="macro-thesis-report__grid">
            {thesis.module_assessments.map((assessment) => (
              <article key={assessment.module_id} data-role={assessment.role}>
                <span>
                  {moduleLabel(assessment.module_id)} · {moduleRoleLabel(assessment.role)}
                </span>
                <p>{assessment.analysis}</p>
                <EvidenceRefs refs={assessment.evidence_refs} citations={citations} />
              </article>
            ))}
          </div>
        ) : (
          <p>Agent 没有把任何模块提升为 material；这不是缺失的六宫格。</p>
        )}
      </section>

      {thesis.material_changes.length ? (
        <section className="macro-thesis-report__section">
          <header>
            <span>WHAT CHANGED</span>
            <h3>相对上一份判断的实质变化</h3>
          </header>
          <ul>
            {thesis.material_changes.map((item) => (
              <li key={item.change_id}>
                <b>{changeStatusLabel(item.status)}</b> {item.statement}
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      <section className="macro-thesis-report__section">
        <header>
          <span>TWELVE-ASSET FACTS · SPARSE OUTLOOK</span>
          <h3>十二资产事实固定呈现，展望只在有传导时出现</h3>
        </header>
        <div
          aria-label="十二资产冻结事实与稀疏展望"
          className="macro-thesis-report__asset-table"
          role="table"
          // Horizontal data grids must be reachable by keyboard for scrolling.
          // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex
          tabIndex={0}
        >
          <div role="row">
            <b role="columnheader">资产 / 冻结日期</b>
            <b role="columnheader">1W 事实</b>
            <b role="columnheader">1M 事实</b>
            <b role="columnheader">Material outlook</b>
          </div>
          {thesis.assets.map((asset) => {
            const outlooks = thesis.asset_outlooks.filter(
              (outlook) => outlook.symbol === asset.symbol,
            );
            return (
              <div data-asset-fact={asset.symbol} key={asset.symbol} role="row">
                <span role="cell">
                  <strong>{asset.symbol}</strong>
                  <small>{asset.as_of ?? "发布时无可用事实"}</small>
                </span>
                <span role="cell">
                  {momentumLabel(asset.momentum_1w)} · {formatReturn(asset.return_1w_pct)}
                </span>
                <span role="cell">
                  {momentumLabel(asset.momentum_1m)} · {formatReturn(asset.return_1m_pct)}
                </span>
                <span role="cell">
                  {outlooks.length ? (
                    <ul>
                      {outlooks.map((outlook) => (
                        <li key={outlook.outlook_id}>
                          <strong>
                            {horizonLabel(outlook.horizon)} ·{" "}
                            {assetDirectionLabel(outlook.direction)}
                          </strong>
                          <small>{outlook.causal_transmission}</small>
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <span aria-label={`${asset.symbol} 本次没有 material outlook`}>—</span>
                  )}
                </span>
              </div>
            );
          })}
        </div>
      </section>

      {thesis.conditions.length || liveDelta ? (
        <section className="macro-thesis-report__section">
          <header>
            <span>LIVE DELTA</span>
            <h3>
              条件化跟踪
              {liveDelta ? ` · ${liveDeltaLabel(liveDelta.mainline_validity)}` : ""}
            </h3>
          </header>
          {thesis.conditions.length ? (
            <div className="macro-thesis-report__condition-list">
              {thesis.conditions.map((condition) => {
                const delta = deltaItems.get(condition.condition_id);
                return (
                  <article key={condition.condition_id}>
                    <strong>{condition.rationale}</strong>
                    <span>
                      {condition.dataset_id
                        ? `${condition.dataset_id} ${condition.metric} ${operatorLabel(condition.operator)} ${condition.threshold}`
                        : `事件 ${condition.event_id}`}
                    </span>
                    <small>{delta ? `当前：${delta.state}` : "尚无当前观察"}</small>
                  </article>
                );
              })}
            </div>
          ) : (
            <p>本次判断没有选择条件，不以通用阈值假装可监控。</p>
          )}
        </section>
      ) : null}

      {recovery.length ? <MacroRecoveryMatrix rows={recovery} /> : null}

      {outcomeReplay ? (
        <section className="macro-thesis-report__section">
          <header>
            <span>OUTCOME REPLAY</span>
            <h3>仅评估 1W / 1M material outlook</h3>
          </header>
          <div className="macro-thesis-report__grid">
            {outcomeReplay.horizons.map((horizon) => (
              <article key={horizon.horizon}>
                <strong>
                  {horizonLabel(horizon.horizon)} · {outcomeStatusLabel(horizon.status)}
                </strong>
                <span>到期 {formatInstant(horizon.expires_at_ms)}</span>
                <small>{horizon.asset_results.length} 个 material 资产</small>
              </article>
            ))}
          </div>
        </section>
      ) : null}

      {!compact ? (
        <section className="macro-thesis-report__section">
          <header>
            <span>AUDIT</span>
            <h3>证据、缺口与生成身份</h3>
          </header>
          <details>
            <summary>{thesis.citations.length} 条实际引用证据</summary>
            <ol>
              {thesis.citations.map((citation) => (
                <li key={citation.evidence_ref}>
                  <strong>{citation.label}</strong>
                  <span>
                    {citation.value ?? "—"} {citation.unit} · {citation.as_of ?? "无日期"}
                  </span>
                </li>
              ))}
            </ol>
          </details>
          <details>
            <summary>{thesis.gaps.length} 个冻结缺口</summary>
            <ol>
              {thesis.gaps.map((gap) => (
                <li key={gap.gap_id}>
                  <strong>{gap.scope_id}</strong>
                  <span>{gap.reason}</span>
                </li>
              ))}
            </ol>
          </details>
          <dl className="macro-thesis-report__provenance">
            <div>
              <dt>ResearchInput</dt>
              <dd>{thesis.research_input_id}</dd>
            </div>
            <div>
              <dt>单次模型</dt>
              <dd>{thesis.provenance.research_model}</dd>
            </div>
            <div>
              <dt>Provider response</dt>
              <dd>{thesis.provenance.provider_response_id}</dd>
            </div>
            <div>
              <dt>Prompt</dt>
              <dd>{thesis.provenance.prompt_version}</dd>
            </div>
            <div>
              <dt>工作流</dt>
              <dd>{thesis.provenance.workflow_version}</dd>
            </div>
            <div>
              <dt>Profile</dt>
              <dd>{thesis.provenance.profile_version}</dd>
            </div>
          </dl>
        </section>
      ) : null}
    </div>
  );
}

export function MacroRecoveryMatrix({ rows }: { rows: MacroRecoveryItem[] }) {
  return (
    <section className="macro-thesis-report__section">
      <header>
        <span>RECOVERY MATRIX</span>
        <h3>发布时缺口与当前事实分开看</h3>
      </header>
      <div
        aria-label="发布时与当前事实恢复矩阵"
        className="macro-thesis-report__recovery"
        role="table"
        // Horizontal data grids must be reachable by keyboard for scrolling.
        // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex
        tabIndex={0}
      >
        <div role="row">
          <b role="columnheader">对象</b>
          <b role="columnheader">状态</b>
          <b role="columnheader">发布时</b>
          <b role="columnheader">当前</b>
        </div>
        {rows.map((item) => (
          <div key={`${item.scope_kind}:${item.scope_id}`} role="row">
            <span role="cell">{item.scope_id}</span>
            <span role="cell">{item.state}</span>
            <span role="cell">{String(item.publication.value ?? "缺失")}</span>
            <span role="cell">{String(item.current.value ?? "缺失")}</span>
          </div>
        ))}
      </div>
    </section>
  );
}

function EvidenceRefs({
  refs,
  citations,
}: {
  refs: string[];
  citations: Map<string, MacroThesisV2["citations"][number]>;
}) {
  return <small>{refs.map((ref) => citations.get(ref)?.label ?? ref).join(" · ")}</small>;
}

function formatReturn(value: number | null): string {
  return value == null ? "—" : `${formatSigned(value)}%`;
}
