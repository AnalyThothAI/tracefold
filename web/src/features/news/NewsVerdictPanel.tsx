import "./newsVerdict.css";
import {
  absoluteTime,
  decisionLabel,
  directionLabel,
  magnitudeLabel,
  optionalTime,
  scopeLabel,
  stageLabel,
} from "./newsLabels";
import type { NewsVerdict } from "./useNewsPage";

const TRIAGE_FIELDS: readonly { key: string; label: string }[] = [
  { key: "event_type", label: "事件类型" },
  { key: "direction", label: "方向" },
  { key: "magnitude", label: "量级" },
  { key: "scope", label: "范围" },
  { key: "actionable", label: "可操作" },
  { key: "confidence", label: "置信度" },
  { key: "audience", label: "受众" },
  { key: "decision", label: "模型建议" },
];

export function NewsVerdictPanel({ verdicts }: { verdicts: readonly NewsVerdict[] }) {
  return (
    <section aria-labelledby="news-verdict-heading" className="news-verdict-panel">
      <header className="news-verdict-panel-header">
        <div>
          <span className="news-eyebrow">VERDICTS</span>
          <h2 id="news-verdict-heading">判定记录</h2>
        </div>
        <span>{verdicts.length} 条</span>
      </header>
      {verdicts.length ? (
        <ol className="news-verdict-list">
          {verdicts.map((verdict) => (
            <li key={`${verdict.stage}:${verdict.policy_version}:${verdict.created_at_ms}`}>
              <NewsVerdictCard verdict={verdict} />
            </li>
          ))}
        </ol>
      ) : (
        <p className="news-verdict-empty">尚无判定；事件仍在 Triage 队列中或未进入候选。</p>
      )}
    </section>
  );
}

function NewsVerdictCard({ verdict }: { verdict: NewsVerdict }) {
  const payload = verdict.verdict ?? {};
  const fields = TRIAGE_FIELDS;
  const headline = stringField(payload, "headline_zh");
  const rationale = stringField(payload, "why_zh");
  const traceEntries = Object.entries(verdict.trace ?? {});
  return (
    <article
      className="news-verdict-card"
      data-decision={verdict.final_decision}
      data-degraded={verdict.degraded ? "true" : "false"}
      data-stage={verdict.stage}
    >
      <header>
        <span className="news-verdict-stage" data-stage={verdict.stage}>
          {stageLabel(verdict.stage)}
        </span>
        <span className="news-verdict-decision" data-decision={verdict.final_decision}>
          最终 {decisionLabel(verdict.final_decision)}
        </span>
        {verdict.degraded ? <span className="news-verdict-flag">降级</span> : null}
        {verdict.error_code ? (
          <span className="news-verdict-flag" data-kind="error">
            {verdict.error_code}
          </span>
        ) : null}
        <time dateTime={new Date(verdict.created_at_ms).toISOString()}>
          {absoluteTime(verdict.created_at_ms)}
        </time>
      </header>
      {headline ? <p className="news-verdict-headline">{headline}</p> : null}
      {rationale ? <p className="news-verdict-rationale">{rationale}</p> : null}
      <dl className="news-verdict-grid">
        <VerdictFact label="规则基线" value={decisionLabel(verdict.rule_baseline_decision)} />
        <VerdictFact
          label="模型判定"
          value={verdict.model_decision ? decisionLabel(verdict.model_decision) : "无"}
        />
        <VerdictFact label="覆写规则" value={verdict.override_rule ?? "无"} />
        <VerdictFact label="节流来源" value={verdict.throttled_by ?? "无"} />
        <VerdictFact label="模型" value={verdict.model ?? "无"} />
        <VerdictFact label="策略版本" value={verdict.policy_version} />
        <VerdictFact label="提示词版本" value={verdict.prompt_version ?? "无"} />
        <VerdictFact label="发布时间" value={optionalTime(verdict.published_at_ms)} />
        {fields.map(({ key, label }) => {
          const value = formatVerdictField(key, payload[key]);
          return value == null ? null : <VerdictFact key={key} label={label} value={value} />;
        })}
      </dl>
      <VerdictAssets payload={payload} />
      <VerdictStringList label="上下文证据" values={payload.context_evidence} />
      {traceEntries.length ? (
        <details className="news-verdict-trace">
          <summary>trace · {traceEntries.length}</summary>
          <dl>
            {traceEntries.map(([key, value]) => (
              <VerdictFact key={key} label={key} value={compactJson(value)} />
            ))}
          </dl>
        </details>
      ) : null}
    </article>
  );
}

function VerdictFact({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}

function VerdictAssets({ payload }: { payload: Record<string, unknown> }) {
  const assets = Array.isArray(payload.assets) ? payload.assets : [];
  const rows = assets.flatMap((asset) => {
    if (!asset || typeof asset !== "object") return [];
    const record = asset as Record<string, unknown>;
    const symbol = typeof record.symbol === "string" ? record.symbol : null;
    if (!symbol) return [];
    const role = typeof record.role === "string" ? record.role : null;
    return [{ role, symbol }];
  });
  if (!rows.length) return null;
  return (
    <ul aria-label="判定资产" className="news-verdict-assets">
      {rows.map(({ role, symbol }) => (
        <li key={`${symbol}:${role ?? ""}`}>
          <b>{symbol}</b>
          {role ? <span>{role}</span> : null}
        </li>
      ))}
    </ul>
  );
}

function VerdictStringList({ label, values }: { label: string; values: unknown }) {
  const items = Array.isArray(values)
    ? values.filter((value): value is string => typeof value === "string" && value.length > 0)
    : [];
  if (!items.length) return null;
  return (
    <div className="news-verdict-evidence">
      <span>{label}</span>
      <ul>
        {items.map((item, index) => (
          <li key={`${index}:${item}`}>{item}</li>
        ))}
      </ul>
    </div>
  );
}

function stringField(payload: Record<string, unknown>, key: string): string | null {
  const value = payload[key];
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

function formatVerdictField(key: string, value: unknown): string | null {
  if (value == null) return null;
  if (typeof value === "boolean") return value ? "是" : "否";
  if (typeof value === "number") {
    if (key.includes("magnitude")) return magnitudeLabel(value) ?? String(value);
    if (key === "confidence") return `${Math.round(value * 100)}%`;
    return String(value);
  }
  if (typeof value !== "string") return compactJson(value);
  if (key.includes("direction")) return directionLabel(value);
  if (key === "scope") return scopeLabel(value);
  if (key === "decision") return decisionLabel(value);
  return value;
}

function compactJson(value: unknown): string {
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}
