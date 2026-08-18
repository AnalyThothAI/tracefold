import { newsPath } from "@shared/routing/paths";
import * as PageState from "@shared/ui/PageState";
import { RouteBackLink } from "@shared/ui/RouteBackLink";
import { Copy, ExternalLink } from "lucide-react";
import { useState } from "react";

import "./news.css";
import "./newsDetail.css";
import { NewsOutcomeBadge } from "./NewsOutcomeBadge";
import { NewsSectionTabs } from "./NewsSectionTabs";
import {
  absoluteTime,
  clockTime,
  displayAssets,
  displayTime,
  optionalTime,
  timelineStageTone,
  validExternalUrl,
} from "./newsLabels";
import {
  type NewsDelivery,
  type NewsEventDetail,
  type NewsEventMember,
  type NewsLabel,
  type NewsTimelineStep,
  type NewsVerdict,
  useNewsEventWithToken,
} from "./useNewsPage";

export function NewsEventDetailPage({ eventId, token }: { eventId: string; token: string }) {
  const query = useNewsEventWithToken(token, eventId);
  const detail = query.data;
  return (
    <section
      aria-label="新闻事件详情"
      className="news-panel news-detail-shell"
      data-page-archetype="case"
    >
      <header className="news-detail-toolbar">
        <RouteBackLink ariaLabel="返回新闻事件流" label="返回事件流" to={newsPath()} />
        <NewsSectionTabs active="event" />
      </header>
      {query.isLoading && !detail ? (
        <PageState.Loading layout="panel" rows={5} label="正在读取事件详情" />
      ) : null}
      {query.isError && !detail ? (
        <PageState.Error error={query.error} onRetry={() => void query.refetch()} />
      ) : null}
      {detail ? <EventDocument detail={detail} /> : null}
    </section>
  );
}

function EventDocument({ detail }: { detail: NewsEventDetail }) {
  const { event, outcome } = detail;
  const triage = detail.verdicts.filter((verdict) => verdict.stage === "triage").at(-1);
  const verdict = (triage?.verdict ?? {}) as {
    headline_zh?: string;
    title_zh?: string;
    why_zh?: string;
  };
  const headline = verdict.headline_zh?.trim() || verdict.title_zh?.trim() || event.leader_title;
  const translated = verdict.title_zh?.trim();
  const url = validExternalUrl(event.leader_url);
  const assets = displayAssets(event.grounded_assets ?? []);
  return (
    <>
      <article className="news-detail-hero">
        <div className="news-detail-hero-top">
          <NewsOutcomeBadge detailed outcome={outcome} size="lg" />
          <time
            className="news-detail-hero-time"
            dateTime={new Date(event.opened_at_ms).toISOString()}
            title={absoluteTime(event.opened_at_ms)}
          >
            {displayTime(event.opened_at_ms)}
          </time>
        </div>
        <h1 className="news-detail-headline">{headline}</h1>
        {translated && translated !== headline ? (
          <p className="news-detail-translated">{translated}</p>
        ) : null}
        {verdict.why_zh ? <p className="news-detail-why">{verdict.why_zh}</p> : null}
        <p className="news-detail-original">
          <span className="news-detail-original-label">原文</span>
          <span>{event.leader_title}</span>
          {url ? (
            <a href={url} rel="noreferrer" target="_blank">
              打开
              <ExternalLink aria-hidden />
            </a>
          ) : null}
        </p>
        <p className="news-detail-facts">
          <span>{event.reporting_origin || "未知来源"}</span>
          {event.member_count > 1 ? <span>{event.member_count} 条同类报道</span> : null}
          {assets.length ? (
            <span aria-label="关联资产" className="news-detail-assets">
              {assets.map((symbol) => (
                <code key={symbol}>{symbol}</code>
              ))}
            </span>
          ) : null}
        </p>
      </article>

      <section aria-label="处理时间线" className="news-card">
        <div className="news-card-header">
          <h2>这条新闻经历了什么</h2>
          <small>每一步一句话；展开可看原始字段</small>
        </div>
        <NewsTimeline steps={detail.timeline ?? []} />
      </section>

      <div className="news-detail-grid">
        <section aria-label="同类报道" className="news-card">
          <div className="news-card-header">
            <h2>同类报道</h2>
            <small>{detail.members.length} 条，按到达时间</small>
          </div>
          <MemberList members={detail.members} />
        </section>
        <section aria-label="运营标注" className="news-card">
          <div className="news-card-header">
            <h2>运营标注</h2>
          </div>
          <LabelList eventId={event.event_id} labels={detail.labels ?? []} />
        </section>
      </div>

      <TechnicalDetails detail={detail} />
    </>
  );
}

function NewsTimeline({ steps }: { steps: NewsTimelineStep[] }) {
  if (!steps.length) return <p className="news-detail-empty">尚无处理记录。</p>;
  return (
    <ol className="news-timeline">
      {steps.map((step, index) => (
        <TimelineStep
          key={`${step.stage}-${index}`}
          last={index === steps.length - 1}
          step={step}
        />
      ))}
    </ol>
  );
}

function TimelineStep({ last, step }: { last: boolean; step: NewsTimelineStep }) {
  const [open, setOpen] = useState(false);
  const facts = Object.entries(step.facts ?? {}).filter(([, value]) => !isEmptyFact(value));
  return (
    <li
      className="news-timeline-step news-toned"
      data-last={last || undefined}
      data-stage={step.stage}
      data-tone={timelineStageTone(step.stage)}
    >
      <span aria-hidden className="news-timeline-marker" />
      <div className="news-timeline-body">
        <div className="news-timeline-head">
          <b>{step.title_zh}</b>
          <time dateTime={new Date(step.at_ms).toISOString()} title={absoluteTime(step.at_ms)}>
            {clockTime(step.at_ms)}
          </time>
        </div>
        <p className="news-timeline-summary">{step.summary_zh}</p>
        {facts.length ? (
          <button
            aria-expanded={open}
            className="news-timeline-toggle"
            onClick={() => setOpen((current) => !current)}
            type="button"
          >
            {open ? "收起字段" : `展开字段 (${facts.length})`}
          </button>
        ) : null}
        {open ? (
          <dl className="news-kv">
            {facts.map(([key, value]) => (
              <div className="news-kv-row" key={key}>
                <dt>{key}</dt>
                <dd>{formatFact(value)}</dd>
              </div>
            ))}
          </dl>
        ) : null}
      </div>
    </li>
  );
}

function MemberList({ members }: { members: NewsEventMember[] }) {
  if (!members.length) return <p className="news-detail-empty">没有成员记录。</p>;
  return (
    <ol className="news-member-list">
      {members.map((member) => {
        const url = validExternalUrl(member.url);
        return (
          <li className="news-member" key={member.item_id}>
            <time
              dateTime={new Date(member.published_at_ms).toISOString()}
              title={absoluteTime(member.published_at_ms)}
            >
              {clockTime(member.published_at_ms)}
            </time>
            <div>
              <p className="news-member-title">{member.title}</p>
              <p className="news-member-meta">
                <span>{member.reporting_origin || "未知来源"}</span>
                {member.match_kind === "leader" ? <span>首条</span> : <span>归并</span>}
                {url ? (
                  <a href={url} rel="noreferrer" target="_blank">
                    原文
                    <ExternalLink aria-hidden />
                  </a>
                ) : null}
              </p>
            </div>
          </li>
        );
      })}
    </ol>
  );
}

function LabelList({ eventId, labels }: { eventId: string; labels: NewsLabel[] }) {
  const command = `tracefold news label ${eventId} --label good|bad|missed`;
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(command);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1_500);
    } catch {
      setCopied(false);
    }
  };
  return (
    <div className="news-label-block">
      {labels.length ? (
        <ul className="news-label-list">
          {labels.map((label, index) => (
            <li key={`${label.created_at_ms}-${index}`}>
              <b>{String(label.label?.label ?? "")}</b>
              {label.label?.note ? <span>{String(label.label.note)}</span> : null}
              <small>
                {label.source} · {absoluteTime(label.created_at_ms)}
              </small>
            </li>
          ))}
        </ul>
      ) : (
        <p className="news-detail-empty">尚无标注。</p>
      )}
      <div className="news-label-command">
        <code>{command}</code>
        <button
          aria-label="复制打标命令"
          className="news-button"
          onClick={() => void copy()}
          type="button"
        >
          <Copy aria-hidden />
          {copied ? "已复制" : "复制"}
        </button>
      </div>
    </div>
  );
}

function TechnicalDetails({ detail }: { detail: NewsEventDetail }) {
  const { event } = detail;
  return (
    <details className="news-technical">
      <summary>技术详情（事件 id、话题线、判定与投递原始记录）</summary>
      <div>
        <section>
          <h4>事件</h4>
          <dl className="news-kv">
            <KV k="event_id" v={event.event_id} />
            <KV k="storyline_key" v={event.storyline_key} />
            <KV k="family" v={event.family} />
            <KV k="admission" v={event.admission} />
            <KV k="priority" v={event.priority} />
            <KV k="engine_type" v={event.engine_type} />
            <KV k="ingest_mode" v={event.ingest_mode} />
            <KV k="asset_class" v={event.asset_class} />
            <KV k="grounded_assets" v={(event.grounded_assets ?? []).join(", ") || "—"} />
            <KV k="watchlist_hits" v={(event.watchlist_hits ?? []).join(", ") || "—"} />
            <KV k="provider_score_max" v={event.provider_score_max ?? "—"} />
            <KV k="provenance" v={(event.provenance ?? []).join(", ") || "—"} />
            <KV k="published_at_ms" v={optionalTime(event.published_at_ms)} />
            <KV k="context_line" v={event.context_line || "—"} />
          </dl>
        </section>
        {detail.verdicts.map((verdict, index) => (
          <VerdictRecord
            key={`${verdict.stage}-${verdict.created_at_ms}-${index}`}
            verdict={verdict}
          />
        ))}
        {detail.deliveries.map((delivery, index) => (
          <DeliveryRecord key={`${delivery.kind}-${index}`} delivery={delivery} />
        ))}
        {detail.members.length ? (
          <section>
            <h4>成员</h4>
            <dl className="news-kv">
              {detail.members.map((member) => (
                <KV
                  k={member.item_id.slice(0, 12)}
                  key={member.item_id}
                  v={`${member.match_kind}${member.jaccard_estimate != null ? ` · jaccard ${member.jaccard_estimate}` : ""} · ${member.reporting_origin}`}
                />
              ))}
            </dl>
          </section>
        ) : null}
      </div>
    </details>
  );
}

function VerdictRecord({ verdict }: { verdict: NewsVerdict }) {
  return (
    <section>
      <h4>判定 · {verdict.stage}</h4>
      <dl className="news-kv">
        <KV k="policy_version" v={verdict.policy_version} />
        <KV k="prompt_version" v={verdict.prompt_version ?? "—"} />
        <KV k="model" v={verdict.model ?? "—"} />
        <KV k="model_decision" v={verdict.model_decision ?? "—"} />
        <KV k="rule_baseline_decision" v={verdict.rule_baseline_decision} />
        <KV k="final_decision" v={verdict.final_decision} />
        <KV k="override_rule" v={verdict.override_rule ?? "—"} />
        <KV k="throttled_by" v={verdict.throttled_by ?? "—"} />
        <KV k="degraded" v={verdict.degraded ? "true" : "false"} />
        <KV k="error_code" v={verdict.error_code ?? "—"} />
        <KV k="created_at_ms" v={absoluteTime(verdict.created_at_ms)} />
        <KV k="published_at_ms" v={optionalTime(verdict.published_at_ms)} />
      </dl>
      <pre className="news-json">
        {JSON.stringify({ verdict: verdict.verdict, trace: verdict.trace }, null, 2)}
      </pre>
    </section>
  );
}

function DeliveryRecord({ delivery }: { delivery: NewsDelivery }) {
  return (
    <section>
      <h4>投递 · {delivery.kind}</h4>
      <dl className="news-kv">
        <KV k="state" v={delivery.state} />
        <KV k="error_code" v={delivery.error_code ?? "—"} />
        <KV k="attempted_at_ms" v={absoluteTime(delivery.attempted_at_ms)} />
        <KV k="settled_at_ms" v={optionalTime(delivery.settled_at_ms)} />
      </dl>
      {delivery.receipt ? (
        <pre className="news-json">{JSON.stringify(delivery.receipt, null, 2)}</pre>
      ) : null}
    </section>
  );
}

function KV({ k, v }: { k: string; v: string | number }) {
  return (
    <div className="news-kv-row">
      <dt>{k}</dt>
      <dd>{String(v)}</dd>
    </div>
  );
}

function isEmptyFact(value: unknown): boolean {
  if (value == null || value === "" || value === false) return true;
  if (Array.isArray(value)) return value.length === 0;
  if (typeof value === "object") return Object.keys(value as object).length === 0;
  return false;
}

function formatFact(value: unknown): string {
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return JSON.stringify(value);
}
