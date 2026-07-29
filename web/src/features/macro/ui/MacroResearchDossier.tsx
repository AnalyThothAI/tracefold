import { useState } from "react";

import {
  assetDirectionLabel,
  changeStatusLabel,
  confidenceLabel,
  formatInstant,
  horizonLabel,
  leadingSideLabel,
  moduleLabel,
  moduleRoleLabel,
  stanceLabel,
} from "../model/macroPresentation";
import type {
  MacroAlternativePresentation,
  MacroAssetPresentation,
  MacroClaimPresentation,
  MacroLiveDeltaReadData,
  MacroMainlinePresentation,
  MacroOutcomeReplayReadData,
  MacroThesisDetailReadData,
  MacroThesisV1,
} from "../model/macroTypes";

import {
  AssetLedger,
  CitationLedger,
  GapLedger,
  LiveDeltaLedger,
  OutcomeLedger,
  PublicationAuditAppendix,
  StatusGlyph,
} from "./MacroResearchAppendices";
import { ConditionList, EvidenceReferences } from "./MacroResearchEvidence";

export function MacroResearchDossier({
  thesis,
  assets,
  claims,
  alternative,
  mainline,
  liveDelta,
  outcomeReplay,
  appendix,
  state,
}: {
  thesis: MacroThesisV1;
  assets: MacroAssetPresentation[];
  claims: MacroClaimPresentation[];
  alternative: MacroAlternativePresentation | null;
  mainline: MacroMainlinePresentation;
  liveDelta: MacroLiveDeltaReadData | null;
  outcomeReplay: MacroOutcomeReplayReadData | null;
  appendix: MacroThesisDetailReadData["appendix"];
  state: MacroThesisDetailReadData["state"];
}) {
  const [auditMounted, setAuditMounted] = useState(false);
  return (
    <article aria-labelledby="macro-research-title" className="macro-research-document">
      <header className="macro-research-document-header">
        <div>
          <span>
            {state === "current" ? "CURRENT" : "HISTORICAL"} · {thesis.session_date}
          </span>
          <h2 id="macro-research-title">{mainline.title.text}</h2>
          <p>{mainline.thesis.text}</p>
        </div>
        <dl>
          <div>
            <dt>市场截止</dt>
            <dd>{formatInstant(thesis.cutoff_ms)}</dd>
          </div>
          <div>
            <dt>Publication</dt>
            <dd>{thesis.publication_id}</dd>
          </div>
          <div>
            <dt>主线</dt>
            <dd>
              {stanceLabel(thesis.mainline.stance)} · {horizonLabel(thesis.mainline.horizon)}
            </dd>
          </div>
          <div>
            <dt>独立 Reviewer</dt>
            <dd>
              <StatusGlyph tone={thesis.review.disposition === "pass" ? "positive" : "negative"} />
              {thesis.review.disposition === "pass" ? "通过" : "未通过"}
            </dd>
          </div>
        </dl>
      </header>

      <nav aria-label="档案论点" className="macro-research-claim-nav">
        <span>论点索引</span>
        {claims.map((claim, index) => (
          <a href={`#macro-claim-${index + 1}`} key={claim.claim_id}>
            论点 {index + 1}
          </a>
        ))}
      </nav>

      <div className="macro-research-sections">
        {claims.map((claim, index) => (
          <ClaimArgument claim={claim} index={index} key={claim.claim_id} thesis={thesis} />
        ))}
        {alternative ? <AlternativeArgument alternative={alternative} thesis={thesis} /> : null}
        {thesis.core_tensions.length ? <TensionLedger thesis={thesis} /> : null}
        {thesis.changes_from_prior.length ? <ChangeLedger thesis={thesis} /> : null}
        <AssetLedger assets={assets} />
        {state === "current" ? (
          <LiveDeltaLedger liveDelta={liveDelta} thesis={thesis} />
        ) : (
          <section className="macro-research-historical-boundary">
            <h3>历史档案边界</h3>
            <p>
              本档案只呈现发布时冻结的结论、数据质量与对账回执，不叠加当前 Live Delta、Outcome
              Replay 或当前模块状态。
            </p>
          </section>
        )}
        {state === "current" ? <OutcomeLedger outcomeReplay={outcomeReplay} /> : null}
        {thesis.gaps.length ? <GapLedger thesis={thesis} /> : null}
      </div>

      <details
        className="macro-research-audit"
        onToggle={(event) => {
          if (event.currentTarget.open) setAuditMounted(true);
        }}
      >
        <summary>发布附录：审阅、数据质量与来源谱系</summary>
        {auditMounted ? (
          <div>
            <h3>Reviewer findings</h3>
            <ul>
              {thesis.review.findings.map((finding) => (
                <li key={finding}>{finding}</li>
              ))}
            </ul>
            <h3>Provenance</h3>
            <Provenance thesis={thesis} />
            <CitationLedger thesis={thesis} />
            {appendix ? <PublicationAuditAppendix appendix={appendix} /> : null}
          </div>
        ) : null}
      </details>
    </article>
  );
}

function ClaimArgument({
  claim,
  index,
  thesis,
}: {
  claim: MacroClaimPresentation;
  index: number;
  thesis: MacroThesisV1;
}) {
  const moduleEvidence = claim.module_evidence;
  const assetImplications = claim.asset_implications;
  const falsifiers = claim.falsifiers;
  const checkpoints = claim.checkpoints;
  return (
    <section className="macro-research-claim" id={`macro-claim-${index + 1}`}>
      <header>
        <span>CLAIM {index + 1}</span>
        <h3>{claim.statement.text}</h3>
      </header>
      {claim.causal_edges.length ? (
        <div aria-label={`论点 ${index + 1} 因果链`} className="macro-research-causal-chain">
          {claim.causal_edges.map((edge) => (
            <p key={`${claim.claim_id}:${edge.source_label}:${edge.target_label}`}>
              <strong>{edge.source_label}</strong>
              <span aria-hidden="true">→</span>
              <span>{edge.mechanism.text}</span>
              <span aria-hidden="true">→</span>
              <strong>{edge.target_label}</strong>
            </p>
          ))}
        </div>
      ) : null}
      <div className="macro-research-claim-evidence">
        <EvidenceReferences
          refs={claim.supporting_evidence_refs}
          thesis={thesis}
          title="支持证据"
        />
        <EvidenceReferences
          refs={claim.conflicting_evidence_refs}
          thesis={thesis}
          title="反向证据"
        />
      </div>
      {moduleEvidence.length ? (
        <div className="macro-research-claim-modules">
          <h4>证据模块如何作用于本论点</h4>
          {moduleEvidence.map((role) => (
            <article data-role={role.role} key={role.module_id}>
              <strong>
                {moduleLabel(role.module_id)} · {moduleRoleLabel(role.role)}
              </strong>
              <p>{role.reader_narrative.text}</p>
            </article>
          ))}
        </div>
      ) : null}
      {assetImplications.length ? (
        <div className="macro-research-claim-assets">
          <h4>资产影响</h4>
          <ul>
            {assetImplications.map((asset) => (
              <li key={`${asset.symbol}:${asset.horizon}`}>
                <strong>
                  {asset.symbol} · {horizonLabel(asset.horizon)}
                </strong>
                <span>
                  {asset.reader_rationale.text} · {assetDirectionLabel(asset.direction)} ·{" "}
                  {confidenceLabel(asset.confidence)}
                </span>
                <EvidenceReferences refs={asset.evidence_links} thesis={thesis} title="资产证据" />
                {asset.confirmation_triggers.length ? (
                  <ConditionList rows={asset.confirmation_triggers} title="确认条件" />
                ) : null}
                {asset.falsifiers.length ? (
                  <ConditionList rows={asset.falsifiers} title="失效条件" />
                ) : null}
                {asset.checkpoints.length ? (
                  <ConditionList rows={asset.checkpoints} title="检查点" />
                ) : null}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      {claim.conditions.length ? <ConditionList rows={claim.conditions} title="论点条件" /> : null}
      {falsifiers.length || checkpoints.length ? (
        <div className="macro-research-condition-grid">
          {falsifiers.length ? <ConditionList rows={falsifiers} title="关联主线失效条件" /> : null}
          {checkpoints.length ? <ConditionList rows={checkpoints} title="关联下一检查点" /> : null}
        </div>
      ) : null}
    </section>
  );
}

function AlternativeArgument({
  alternative,
  thesis,
}: {
  alternative: MacroAlternativePresentation;
  thesis: MacroThesisV1;
}) {
  return (
    <section className="macro-research-alternative">
      <header>
        <span>ALTERNATIVE</span>
        <h3>备选解释：{alternative.title.text}</h3>
      </header>
      <p>{alternative.thesis.text}</p>
      {alternative.causal_edges.map((edge) => (
        <p
          className="macro-research-inline-chain"
          key={`${edge.source_label}:${edge.target_label}`}
        >
          {edge.source_label} → {edge.mechanism.text} → {edge.target_label}
        </p>
      ))}
      <div className="macro-research-claim-evidence">
        <EvidenceReferences
          refs={alternative.supporting_evidence_refs}
          thesis={thesis}
          title="支持"
        />
        <EvidenceReferences
          refs={alternative.conflicting_evidence_refs}
          thesis={thesis}
          title="反向证据"
        />
      </div>
      {alternative.trigger_conditions.length ? (
        <ConditionList rows={alternative.trigger_conditions} title="转向该解释的条件" />
      ) : null}
    </section>
  );
}

function TensionLedger({ thesis }: { thesis: MacroThesisV1 }) {
  return (
    <section>
      <h3>核心矛盾与解决条件</h3>
      <div className="macro-research-tensions">
        {thesis.core_tensions.map((tension) => (
          <article key={tension.tension_id}>
            <strong>{tension.statement}</strong>
            <div>
              <section data-leading={tension.leading_side === "side_a" || undefined}>
                <p>
                  <span>{tension.side_a.label}</span>
                  {tension.side_a.statement}
                </p>
                <EvidenceReferences
                  refs={tension.side_a.evidence_refs}
                  thesis={thesis}
                  title={`${tension.side_a.label}证据`}
                />
              </section>
              <section data-leading={tension.leading_side === "side_b" || undefined}>
                <p>
                  <span>{tension.side_b.label}</span>
                  {tension.side_b.statement}
                </p>
                <EvidenceReferences
                  refs={tension.side_b.evidence_refs}
                  thesis={thesis}
                  title={`${tension.side_b.label}证据`}
                />
              </section>
            </div>
            <small>
              当前领先：
              {leadingSideLabel(tension.leading_side, tension.side_a.label, tension.side_b.label)}
              ；滞后信号：{tension.lagging_signal}；尚未解决：{tension.unresolved_reason}
            </small>
            {tension.resolution_triggers.length ? (
              <ConditionList rows={tension.resolution_triggers} title="解决条件" />
            ) : null}
          </article>
        ))}
      </div>
    </section>
  );
}

function ChangeLedger({ thesis }: { thesis: MacroThesisV1 }) {
  return (
    <section>
      <h3>相较上一期的变化</h3>
      <ol className="macro-research-changes">
        {thesis.changes_from_prior.map((change) => (
          <li data-status={change.status} key={change.change_id}>
            <strong>{changeStatusLabel(change.status)}</strong>
            <span>{change.statement}</span>
          </li>
        ))}
      </ol>
    </section>
  );
}

function Provenance({ thesis }: { thesis: MacroThesisV1 }) {
  return (
    <dl className="macro-research-provenance">
      <div>
        <dt>Evidence Pack</dt>
        <dd>{thesis.evidence_pack_id}</dd>
      </div>
      <div>
        <dt>Workflow</dt>
        <dd>{thesis.provenance.workflow_version}</dd>
      </div>
      <div>
        <dt>Research model</dt>
        <dd>{thesis.provenance.research_model}</dd>
      </div>
      <div>
        <dt>Research prompt</dt>
        <dd>{thesis.provenance.research_prompt_version}</dd>
      </div>
      <div>
        <dt>Research invocation</dt>
        <dd>{thesis.provenance.research_invocation_id}</dd>
      </div>
      <div>
        <dt>Reviewer model</dt>
        <dd>{thesis.provenance.reviewer_model}</dd>
      </div>
      <div>
        <dt>Reviewer prompt</dt>
        <dd>{thesis.provenance.reviewer_prompt_version}</dd>
      </div>
      <div>
        <dt>Draft hash</dt>
        <dd>{thesis.provenance.draft_hash}</dd>
      </div>
    </dl>
  );
}
