import { ExternalLink } from "lucide-react";

import {
  conditionEffectLabel,
  formatInstant,
  formatNumber,
  moduleLabel,
  operatorLabel,
} from "../model/macroPresentation";
import type { MacroThesisV1 } from "../model/macroTypes";

export function EvidenceReferences({
  refs,
  thesis,
  title,
}: {
  refs: string[];
  thesis: MacroThesisV1;
  title: string;
}) {
  const citations = new Map(thesis.citations.map((citation) => [citation.evidence_ref, citation]));
  return (
    <section className="macro-research-evidence-list">
      <h4>{title}</h4>
      {refs.length ? (
        <ul>
          {refs.map((ref) => {
            const citation = citations.get(ref);
            return citation ? (
              <CitationItem citation={citation} key={ref} />
            ) : (
              <li data-state="missing" key={ref}>
                <strong>引用未闭合</strong>
                <span>该证据未在本次 publication 的 citation ledger 中找到。</span>
              </li>
            );
          })}
        </ul>
      ) : (
        <p>本论点没有登记此类证据。</p>
      )}
    </section>
  );
}

export function CitationItem({
  citation,
  showAuditIdentity = false,
}: {
  citation: MacroThesisV1["citations"][number];
  showAuditIdentity?: boolean;
}) {
  return (
    <li>
      <div>
        <strong>{citation.label}</strong>
        <span>
          {moduleLabel(citation.module_id)}
          {citation.reference ? ` · ${citation.reference}` : ""}
        </span>
        {citation.published_at_ms || citation.received_at_ms ? (
          <small>
            {citation.published_at_ms
              ? `发布 ${formatInstant(citation.published_at_ms)}`
              : "未提供发布时间"}
            {citation.received_at_ms ? ` · 接收 ${formatInstant(citation.received_at_ms)}` : ""}
          </small>
        ) : null}
      </div>
      {citation.source_url ? (
        <a href={citation.source_url} rel="noreferrer" target="_blank">
          查看来源 <ExternalLink aria-hidden="true" />
        </a>
      ) : null}
      {showAuditIdentity ? (
        <details className="macro-research-evidence-identity">
          <summary>查看引用技术身份</summary>
          <code>Evidence ref {citation.evidence_ref}</code>
          {citation.dataset_id ? <code>Dataset {citation.dataset_id}</code> : null}
          {citation.source_role ? (
            <small>来源：{sourceRoleLabel(citation.source_role)}</small>
          ) : null}
        </details>
      ) : null}
    </li>
  );
}

function sourceRoleLabel(value: string): string {
  return (
    {
      decision_primary: "决策主源",
      derived: "派生事实",
      history: "历史序列",
      intraday_proxy: "盘中代理",
      official_document: "官方文件",
      reconciliation_only: "对账来源",
      release: "官方发布",
    }[value] ?? "其他登记来源"
  );
}

export function ConditionList({
  title,
  rows,
}: {
  title: string;
  rows: MacroThesisV1["mainline"]["falsifiers"];
}) {
  return (
    <section className="macro-research-conditions">
      <h4>{title}</h4>
      {rows.length ? (
        <ul>
          {rows.map((row) => (
            <li data-effect={row.effect} key={row.condition_id}>
              <strong>{conditionEffectLabel(row.effect)}</strong>
              <span>{row.rationale}</span>
              <small>
                观察阈值 {operatorLabel(row.operator)} {formatNumber(row.threshold)}
              </small>
            </li>
          ))}
        </ul>
      ) : (
        <p>本次 publication 未登记此类条件。</p>
      )}
    </section>
  );
}
