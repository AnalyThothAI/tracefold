import { newsPath } from "@shared/routing/paths";
import { ActionButton } from "@shared/ui/ActionButton";
import { Card } from "@shared/ui/Card";
import { FactGrid } from "@shared/ui/FactGrid";
import { KeyValue, KeyValueRow } from "@shared/ui/KeyValue";
import * as PageState from "@shared/ui/PageState";
import { RouteBackLink } from "@shared/ui/RouteBackLink";
import { ArrowRight, ExternalLink } from "lucide-react";
import { useLocation } from "react-router-dom";

import {
  type NewsDelivery,
  type NewsEventDetail,
  type NewsEventMember,
  type NewsEventReaction,
  type NewsLabel,
  type NewsQuote,
  type NewsReaction,
  type NewsSymbolNormalization,
  type NewsTriageSummary,
  type NewsVerdict,
  useNewsEventWithToken,
  useNewsQuotesWithToken,
} from "../../api/newsQueries";
import {
  absoluteTime,
  clockTime,
  displayAssetRefs,
  labelCommand,
  optionalTime,
  timelineEndToEnd,
  validExternalUrl,
} from "../../model/newsLabels";
import { NewsAssetChips } from "../chrome/NewsAssetChips";
import { NewsEmptyNote, NewsPageShell, NewsTechnical } from "../chrome/NewsChrome";
import { NewsDirectionChip } from "../chrome/NewsDirectionChip";
import { NewsOutcomeBadge } from "../chrome/NewsOutcomeBadge";
import { NewsReactionValue } from "../chrome/NewsQuoteValue";

import { NewsEventPager } from "./NewsEventPager";
import { NewsQuoteTable } from "./NewsQuoteTable";
import { NewsTimeline } from "./NewsTimeline";

import "./newsDetail.css";

// Every key here is a value `tracefold news label` accepts as its positional argument; `must_push` is the one
// that fills the release gate's boundary set (#81).
const LABEL_ACTIONS = [
  { key: "good", label: "判得对", note: "已复制「判得对」标注命令", variant: "positive" },
  { key: "noise", label: "不该推", note: "已复制「不该推」标注命令", variant: "negative" },
  { key: "missed", label: "漏推", note: "已复制「漏推」标注命令", variant: "secondary" },
  {
    key: "must_push",
    label: "必须推",
    note: "已复制「必须推」标注命令（发布门边界集）",
    variant: "secondary",
  },
] as const;

export function NewsEventDetailPage({
  copy,
  eventId,
  token,
}: {
  copy: (text: string, note: string) => void;
  eventId: string;
  token: string;
}) {
  const query = useNewsEventWithToken(token, eventId);
  const detail = query.data;
  // The feed the reader came from, so 上一条/下一条 walk the list they were actually looking at. A cold URL
  // has no such list; the pager hides itself rather than inventing one.
  const feedSearch = (useLocation().state as { feedSearch?: string } | null)?.feedSearch ?? null;
  // The same batched quote query the feed uses (#88); on this route the batch is one Event's assets, and
  // React Query serves both from one cache entry when the symbols happen to match.
  const quotesQuery = useNewsQuotesWithToken(
    token,
    (detail?.event.assets ?? []).filter((asset) => asset.listed).map((asset) => asset.symbol),
  );
  const quotes = Object.fromEntries(
    (quotesQuery.data?.quotes ?? []).map((quote) => [quote.requested_symbol, quote]),
  );
  return (
    <NewsPageShell archetype="case" className="news-detail-shell" label="新闻事件详情">
      <header className="news-detail-toolbar">
        <RouteBackLink
          ariaLabel="返回新闻事件流"
          label="事件流"
          to={feedSearch ? `${newsPath()}?${feedSearch}` : newsPath()}
        />
        <NewsEventPager eventId={eventId} feedSearch={feedSearch} token={token} />
      </header>
      {query.isLoading && !detail ? (
        <PageState.Loading label="正在读取事件详情" layout="panel" rows={5} />
      ) : null}
      {query.isError && !detail ? (
        <PageState.Error error={query.error} onRetry={() => void query.refetch()} />
      ) : null}
      {detail ? <EventDocument copy={copy} detail={detail} quotes={quotes} /> : null}
    </NewsPageShell>
  );
}

function EventDocument({
  copy,
  detail,
  quotes,
}: {
  copy: (text: string, note: string) => void;
  detail: NewsEventDetail;
  quotes: Record<string, NewsQuote>;
}) {
  const { event, outcome, triage } = detail;
  const headline = triage?.headline_zh?.trim() || triage?.title_zh?.trim() || event.leader_title;
  const translated = triage?.title_zh?.trim();
  const url = validExternalUrl(event.leader_url);
  const assets = displayAssetRefs(event.grounded_assets ?? [], event.assets);
  const quoteList = assets.map((asset) => quotes[asset.symbol]).filter(Boolean);
  const steps = detail.timeline ?? [];
  return (
    <>
      <article className="news-detail-hero" data-direction={triage?.direction ?? undefined}>
        <div className="news-detail-hero-top">
          {/* The conclusion and its one-line why, side by side: the chip is the verdict, the sentence is the
              server's reason for it. The chip does not repeat the reason inside itself. */}
          <span className="news-detail-hero-state">
            <NewsOutcomeBadge outcome={outcome} size="lg" variant="chip" />
            {outcome.reason_zh ? <span>{outcome.reason_zh}</span> : null}
          </span>
          <time
            className="news-detail-hero-time"
            dateTime={new Date(event.opened_at_ms).toISOString()}
            title={absoluteTime(event.opened_at_ms)}
          >
            {absoluteTime(event.opened_at_ms).slice(11)} · {timelineEndToEnd(steps)}
          </time>
        </div>

        <h1 className="news-detail-headline">{headline}</h1>
        {translated && translated !== headline ? (
          <p className="news-detail-translated">{translated}</p>
        ) : null}

        {triage || assets.length ? (
          <div aria-label="模型判定" className="news-detail-verdict">
            {triage ? <NewsDirectionChip size="lg" triage={triage} /> : null}
            {assets.length ? (
              <>
                <span aria-hidden className="news-detail-rule" />
                <NewsAssetChips assets={assets} quotes={quotes} />
              </>
            ) : null}
          </div>
        ) : null}

        {quoteList.length ? <NewsQuoteTable quotes={quoteList} /> : null}

        {triage?.why_zh ? <p className="news-detail-why">{triage.why_zh}</p> : null}
        {triage ? <VerdictFacts triage={triage} /> : null}
        {triage ? <ModelIntent triage={triage} /> : null}

        <p className="news-detail-original">
          <span className="news-detail-original-label">
            原文 · {event.reporting_origin || "未知来源"}
            {event.member_count > 1 ? ` · ${event.member_count} 条报道` : ""}
          </span>
          <span>{event.leader_title}</span>
          {url ? (
            <a href={url} rel="noreferrer" target="_blank">
              打开
              <ExternalLink aria-hidden />
            </a>
          ) : null}
        </p>
      </article>

      <SymbolNormalization groups={detail.normalization ?? []} />

      {/* The second market block, deliberately its own card: "now" and "after this Event" are different time
          semantics, and one table would invite reading a rolling change as the market's answer to this news. */}
      <Card
        aria-label="事件后反应"
        hint="以新闻发布时间为锚点的固定收益，不是当前滚动涨跌"
        title="事件后反应"
      >
        <EventReactions aggregate={detail.reaction} reactions={detail.reactions ?? []} />
      </Card>

      <div className="news-detail-grid">
        <Card
          aria-label="处理时间线"
          className="news-detail-timeline-card"
          hint={timelineEndToEnd(steps)}
          title="这条新闻经历了什么"
        >
          <NewsTimeline steps={steps} />
        </Card>

        <div className="news-detail-side">
          <Card aria-label="运营标注" hint="按钮复制 CLI 命令，不走写接口" title="这条判得对吗">
            <LabelBlock copy={copy} eventId={event.event_id} labels={detail.labels ?? []} />
          </Card>
          <Card
            aria-label="同类报道"
            hint={`${detail.members.length} 条，按到达时间`}
            title="同类报道"
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
 * 事件后反应 (#88): the deterministic return between this Event's anchor and each horizon, per asset.
 *
 * The raw closes and their timestamps ship beside the returns so the number is auditable rather than
 * asserted. A horizon that has not matured says so; a gap the provider has no bar for says that instead of
 * forward-filling a price across it.
 */
function EventReactions({
  aggregate,
  reactions,
}: {
  aggregate: NewsReaction | null | undefined;
  reactions: NewsEventReaction[];
}) {
  if (!reactions.length && !aggregate) {
    return <NewsEmptyNote>还没有可用的事件后反应。</NewsEmptyNote>;
  }
  return (
    <div className="news-detail-reactions">
      {aggregate ? (
        <p className="news-detail-reaction-aggregate">
          <span>事件级（主标的中位）</span>
          <NewsReactionValue horizon="1h" reaction={aggregate} />
          <NewsReactionValue horizon="4h" reaction={aggregate} />
          <small>
            {aggregate.priced_n}/{aggregate.asset_n} 个主标的已定价 · {aggregate.metric_version}
          </small>
        </p>
      ) : null}
      {reactions.length ? (
        <ul className="news-detail-reaction-list">
          {reactions.map((reaction) => (
            <li key={reaction.symbol}>
              <span className="news-detail-quote-symbol">
                <code>{reaction.symbol}</code>
                {reaction.venue ? (
                  <small>
                    {reaction.venue}:{reaction.venue_symbol}
                  </small>
                ) : null}
              </span>
              <NewsReactionValue horizon="1h" reaction={reaction} />
              <NewsReactionValue horizon="4h" reaction={reaction} />
              <small className="news-detail-reaction-closes">
                {reaction.p0 ? `p0 ${reaction.p0}` : reaction.state_zh}
                {reaction.p1 ? ` · p1 ${reaction.p1}` : ""}
                {reaction.p4 ? ` · p4 ${reaction.p4}` : ""}
                {reaction.unavailable_reason_zh ? ` · ${reaction.unavailable_reason_zh}` : ""}
              </small>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

/**
 * The rest of the model's judgment, in the server's own words. A cell is omitted rather than rendered as a
 * dash when the server has nothing for it, so a macro Event with no assets does not show a row of dashes.
 */
function VerdictFacts({ triage }: { triage: NewsTriageSummary }) {
  return (
    <FactGrid
      className="news-detail-fact-grid"
      facts={[
        { label: "类型", value: triage.event_type_zh },
        { label: "范围", value: triage.scope_zh },
        // Confidence used to sit beside the direction, where it competed with the one number that matters
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
      ]}
      label="判定明细"
    />
  );
}

/** Which assets the model itself called primary, and which it merely mentioned. */
function ModelIntent({ triage }: { triage: NewsTriageSummary }) {
  const assets = triage.assets ?? [];
  const primary = assets.filter((asset) => asset.role === "primary").map((a) => a.symbol);
  const mentioned = assets.filter((asset) => asset.role !== "primary").map((a) => a.symbol);
  const overruled = triage.model_decision && triage.model_decision !== triage.final_decision;
  if (!primary.length && !mentioned.length && !overruled) return null;
  return (
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
      {/* Shown only when the deterministic policy overruled the model — otherwise the outcome already said it. */}
      {overruled ? (
        <span className="news-detail-asset-group">
          <small>模型建议</small>
          <b>{triage.model_decision_zh || triage.model_decision}</b>
          <small>最终</small>
          <b>{triage.decision_zh || triage.final_decision}</b>
        </span>
      ) : null}
    </p>
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
      aria-label="符号归一"
      className="news-detail-normalization"
      flush
      hint="节流键按 base_symbol 分桶，不按合约"
      title="符号归一"
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
                <span>{member.match_kind === "leader" ? "首条" : "归并"}</span>
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

/**
 * Labels are the whole learning plane, and they are written by `tracefold news label` — the API is read-only
 * by design. So each button hands the operator the exact command instead of pretending to write one.
 */
function LabelBlock({
  copy,
  eventId,
  labels,
}: {
  copy: (text: string, note: string) => void;
  eventId: string;
  labels: NewsLabel[];
}) {
  return (
    <div className="news-label-block">
      <div className="news-label-actions">
        {LABEL_ACTIONS.map((action) => (
          <ActionButton
            key={action.key}
            onClick={() => copy(labelCommand(eventId, action.key), action.note)}
            variant={action.variant}
          >
            {action.label}
          </ActionButton>
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
        <DeliveryRecord delivery={delivery} key={`${delivery.kind}-${index}`} />
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
