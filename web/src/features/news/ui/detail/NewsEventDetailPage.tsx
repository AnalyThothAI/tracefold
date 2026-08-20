import { newsPath } from "@shared/routing/paths";
import { Card } from "@shared/ui/Card";
import { FactGrid } from "@shared/ui/FactGrid";
import { KeyValue, KeyValueRow } from "@shared/ui/KeyValue";
import * as PageState from "@shared/ui/PageState";
import { RouteBackLink } from "@shared/ui/RouteBackLink";
import { ArrowRight, ExternalLink, Tag } from "lucide-react";
import { useLocation } from "react-router-dom";

import {
  type NewsDelivery,
  type NewsEventDetail,
  type NewsEventMember,
  type NewsLabel,
  type NewsSymbolNormalization,
  type NewsTriageSummary,
  type NewsVerdict,
  useNewsEventWithToken,
} from "../../api/newsQueries";
import {
  absoluteTime,
  clockTime,
  displayAssetRefs,
  displayTime,
  labelCommand,
  optionalTime,
  timelineEndToEnd,
  validExternalUrl,
} from "../../model/newsLabels";
import { useNewsToast } from "../../state/useNewsToast";
import { NewsAssetChips } from "../chrome/NewsAssetChips";
import { NewsEmptyNote, NewsPageShell, NewsTechnical } from "../chrome/NewsChrome";
import { NewsDirectionChip } from "../chrome/NewsDirectionChip";
import { NewsOutcomeBadge } from "../chrome/NewsOutcomeBadge";
import { NewsToast } from "../chrome/NewsToast";

import { NewsEventPager } from "./NewsEventPager";
import { NewsTimeline } from "./NewsTimeline";

import "./newsDetail.css";

export function NewsEventDetailPage({ eventId, token }: { eventId: string; token: string }) {
  const query = useNewsEventWithToken(token, eventId);
  const detail = query.data;
  const toast = useNewsToast();
  // The feed the reader came from, so 上一条/下一条 walk the list they were actually looking at. A cold URL
  // has no such list; the pager hides itself rather than inventing one.
  const feedSearch = (useLocation().state as { feedSearch?: string } | null)?.feedSearch ?? null;
  return (
    <NewsPageShell archetype="case" className="news-detail-shell" label="新闻事件详情">
      <header className="news-detail-toolbar">
        <RouteBackLink
          ariaLabel="返回新闻事件流"
          label="返回事件流"
          to={feedSearch ? `${newsPath()}?${feedSearch}` : newsPath()}
        />
        <NewsEventPager eventId={eventId} feedSearch={feedSearch} token={token} />
      </header>
      {query.isLoading && !detail ? (
        <PageState.Loading layout="panel" rows={5} label="正在读取事件详情" />
      ) : null}
      {query.isError && !detail ? (
        <PageState.Error error={query.error} onRetry={() => void query.refetch()} />
      ) : null}
      {detail ? <EventDocument detail={detail} onCopy={toast.copy} /> : null}
      <NewsToast message={toast.message} />
    </NewsPageShell>
  );
}

function EventDocument({
  detail,
  onCopy,
}: {
  detail: NewsEventDetail;
  onCopy: (text: string, note: string) => void;
}) {
  const { event, outcome, triage } = detail;
  const headline = triage?.headline_zh?.trim() || triage?.title_zh?.trim() || event.leader_title;
  const translated = triage?.title_zh?.trim();
  const url = validExternalUrl(event.leader_url);
  const assets = displayAssetRefs(event.grounded_assets ?? [], event.assets);
  const steps = detail.timeline ?? [];
  return (
    <>
      <article className="news-detail-hero" data-direction={triage?.direction ?? undefined}>
        <div className="news-detail-hero-top">
          <NewsOutcomeBadge
            detailed
            emphasis={event.priority === "high" && outcome.group === "pushed" ? "solid" : "soft"}
            outcome={outcome}
            size="lg"
          />
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
        {triage ? (
          <div aria-label="模型判定" className="news-detail-verdict">
            <NewsDirectionChip size="lg" triage={triage} />
          </div>
        ) : null}
        {triage?.why_zh ? <p className="news-detail-why">{triage.why_zh}</p> : null}
        {triage ? <VerdictFacts triage={triage} /> : null}
        {triage ? <ModelIntent triage={triage} /> : null}
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
          <NewsAssetChips assets={assets} />
        </p>
      </article>

      <SymbolNormalization groups={detail.normalization ?? []} />

      <div className="news-detail-grid">
        <Card
          title="这条新闻经历了什么"
          hint={timelineEndToEnd(steps)}
          aria-label="处理时间线"
          className="news-detail-timeline-card"
        >
          <NewsTimeline steps={steps} />
        </Card>

        <div className="news-detail-side">
          <Card title="这条判得对吗" hint="标注写回模型阈值的回归集" aria-label="运营标注">
            <LabelBlock eventId={event.event_id} labels={detail.labels ?? []} onCopy={onCopy} />
          </Card>
          <Card
            title="同类报道"
            hint={<>{detail.members.length} 条，按到达时间</>}
            aria-label="同类报道"
          >
            <MemberList members={detail.members} />
          </Card>
        </div>
      </div>

      <TechnicalDetails detail={detail} />
    </>
  );
}

/**
 * The rest of the model's judgment, in the server's own words. A cell is omitted rather than rendered as a dash
 * when the server has nothing for it, so a macro Event with no assets does not show an empty row.
 */
function VerdictFacts({ triage }: { triage: NewsTriageSummary }) {
  const assets = triage.assets ?? [];
  const primary = assets.filter((asset) => asset.role === "primary").map((a) => a.symbol);
  const mentioned = assets.filter((asset) => asset.role !== "primary").map((a) => a.symbol);
  const facts = [
    { label: "类型", value: triage.event_type_zh },
    { label: "范围", value: triage.scope_zh },
    // Confidence used to sit beside the direction chip, where it competed with the one number that matters
    // there. It is a judgment detail like the rest, so it reads as one (#87).
    {
      label: "把握",
      value: triage.confidence == null ? "" : `${Math.round(triage.confidence * 100)}%`,
    },
    { label: "新颖度", value: triage.novelty_zh },
    {
      label: "可操作",
      value: triage.actionable == null ? "" : triage.actionable ? "是" : "否",
    },
    { label: "受众", value: triage.audience_zh },
  ];
  return (
    <>
      <FactGrid className="news-detail-fact-grid" facts={facts} label="判定明细" />
      {primary.length || mentioned.length ? (
        <p className="news-detail-intent">
          {primary.length ? (
            <span className="news-detail-asset-group">
              <small>主要标的</small>
              {primary.map((symbol) => (
                <code key={symbol}>{symbol}</code>
              ))}
            </span>
          ) : null}
          {mentioned.length ? (
            <span className="news-detail-asset-group">
              <small>提及</small>
              {mentioned.map((symbol) => (
                <code key={symbol}>{symbol}</code>
              ))}
            </span>
          ) : null}
        </p>
      ) : null}
    </>
  );
}

/**
 * Why several contracts share one storyline bucket (#87). The server only sends a group when it actually
 * collapses more than one name, so this renders nothing for the ordinary Event whose ticker answers to
 * itself — the block exists to explain a surprise, not to restate the obvious.
 *
 * The 2026-08-19 failure it makes visible: one SK Hynix buyback shipped nine cards because the provider
 * alternated between SKHY, SKHX and SKHYNIX.
 */
function SymbolNormalization({ groups }: { groups: NewsSymbolNormalization[] }) {
  if (!groups.length) return null;
  return (
    <Card
      title="符号归一"
      hint="节流键按 base_symbol 分桶，不按合约"
      aria-label="符号归一"
      className="news-detail-normalization"
    >
      <ul>
        {groups.map((group) => (
          <li key={group.base_symbol}>
            <span className="news-normalization-aliases">
              {(group.aliases ?? []).map((alias) => (
                <code key={alias}>{alias}</code>
              ))}
            </span>
            <ArrowRight aria-hidden />
            <code className="news-normalization-base">{group.base_symbol}</code>
          </li>
        ))}
      </ul>
    </Card>
  );
}

/** Shown only when the deterministic policy overruled the model — otherwise the outcome badge already said it. */
function ModelIntent({ triage }: { triage: NewsTriageSummary }) {
  if (!triage.model_decision || triage.model_decision === triage.final_decision) return null;
  return (
    <p className="news-detail-intent">
      <span>模型建议</span>
      <b>{triage.model_decision_zh || triage.model_decision}</b>
      <span>→ 最终</span>
      <b>{triage.decision_zh || triage.final_decision}</b>
    </p>
  );
}

function MemberList({ members }: { members: NewsEventMember[] }) {
  if (!members.length) return <NewsEmptyNote>没有成员记录。</NewsEmptyNote>;
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

// Every key here is a value `tracefold news label` accepts as its positional argument; `must_push` is the one
// that fills the release gate's boundary set (#81).
const LABEL_ACTIONS = [
  { key: "good", label: "判得对", note: "已复制「判得对」标注命令" },
  { key: "noise", label: "不该推", note: "已复制「不该推」标注命令" },
  { key: "missed", label: "漏推", note: "已复制「漏推」标注命令" },
  { key: "must_push", label: "必须推", note: "已复制「必须推」标注命令（发布门边界集）" },
] as const;

/**
 * Labels are the whole learning plane, and they are written by `tracefold news label` — the API is read-only
 * by design. So each button hands the operator the exact command instead of pretending to write one.
 */
function LabelBlock({
  eventId,
  labels,
  onCopy,
}: {
  eventId: string;
  labels: NewsLabel[];
  onCopy: (text: string, note: string) => void;
}) {
  return (
    <div className="news-label-block">
      <div className="news-label-actions">
        {LABEL_ACTIONS.map((action) => (
          <button
            className="news-label-action"
            data-label={action.key}
            key={action.key}
            onClick={() => onCopy(labelCommand(eventId, action.key), action.note)}
            type="button"
          >
            <Tag aria-hidden />
            {action.label}
          </button>
        ))}
      </div>
      <p className="news-label-hint">
        点一下复制命令，在终端里执行即可写入标注。快捷键 <kbd>X</kbd> 复制「不该推」。
      </p>
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
        <NewsEmptyNote>尚无标注。</NewsEmptyNote>
      )}
    </div>
  );
}

function TechnicalDetails({ detail }: { detail: NewsEventDetail }) {
  const { event } = detail;
  return (
    <NewsTechnical summary="技术详情（事件 id、话题线、判定与投递原始记录）">
      <section>
        <h4>事件</h4>
        <KeyValue>
          <KeyValueRow k="event_id" v={event.event_id} />
          <KeyValueRow k="storyline_key" v={event.storyline_key} />
          <KeyValueRow k="family" v={event.family} />
          <KeyValueRow k="admission" v={event.admission} />
          <KeyValueRow k="priority" v={event.priority} />
          <KeyValueRow k="engine_type" v={event.engine_type} />
          <KeyValueRow k="ingest_mode" v={event.ingest_mode} />
          <KeyValueRow k="asset_class" v={event.asset_class} />
          <KeyValueRow k="grounded_assets" v={(event.grounded_assets ?? []).join(", ") || "—"} />
          <KeyValueRow k="watchlist_hits" v={(event.watchlist_hits ?? []).join(", ") || "—"} />
          <KeyValueRow k="provider_score_max" v={String(event.provider_score_max ?? "—")} />
          <KeyValueRow k="provenance" v={(event.provenance ?? []).join(", ") || "—"} />
          <KeyValueRow k="published_at_ms" v={optionalTime(event.published_at_ms)} />
          <KeyValueRow k="context_line" v={event.context_line || "—"} />
        </KeyValue>
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
          <KeyValue>
            {detail.members.map((member) => (
              <KeyValueRow
                k={member.item_id.slice(0, 12)}
                key={member.item_id}
                v={`${member.match_kind}${member.jaccard_estimate != null ? ` · jaccard ${member.jaccard_estimate}` : ""} · ${member.reporting_origin}`}
              />
            ))}
          </KeyValue>
        </section>
      ) : null}
    </NewsTechnical>
  );
}

function VerdictRecord({ verdict }: { verdict: NewsVerdict }) {
  return (
    <section>
      <h4>判定 · {verdict.stage}</h4>
      <KeyValue>
        <KeyValueRow k="policy_version" v={verdict.policy_version} />
        <KeyValueRow k="prompt_version" v={verdict.prompt_version ?? "—"} />
        <KeyValueRow k="model" v={verdict.model ?? "—"} />
        <KeyValueRow k="model_decision" v={verdict.model_decision ?? "—"} />
        <KeyValueRow k="rule_baseline_decision" v={verdict.rule_baseline_decision} />
        <KeyValueRow k="final_decision" v={verdict.final_decision} />
        <KeyValueRow k="override_rule" v={verdict.override_rule ?? "—"} />
        <KeyValueRow k="throttled_by" v={verdict.throttled_by ?? "—"} />
        <KeyValueRow k="degraded" v={verdict.degraded ? "true" : "false"} />
        <KeyValueRow k="error_code" v={verdict.error_code ?? "—"} />
        <KeyValueRow k="created_at_ms" v={absoluteTime(verdict.created_at_ms)} />
        <KeyValueRow k="published_at_ms" v={optionalTime(verdict.published_at_ms)} />
      </KeyValue>
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
      <KeyValue>
        <KeyValueRow k="state" v={delivery.state} />
        <KeyValueRow k="error_code" v={delivery.error_code ?? "—"} />
        <KeyValueRow k="attempted_at_ms" v={absoluteTime(delivery.attempted_at_ms)} />
        <KeyValueRow k="settled_at_ms" v={optionalTime(delivery.settled_at_ms)} />
      </KeyValue>
      {delivery.receipt ? (
        <pre className="news-json">{JSON.stringify(delivery.receipt, null, 2)}</pre>
      ) : null}
    </section>
  );
}
