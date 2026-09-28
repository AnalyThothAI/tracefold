import { newsEventPath, newsPath, newsSymbolPath } from "@shared/routing/paths";
import { useRouteReferrer } from "@shared/routing/routeReferrer";
import { Card } from "@shared/ui/Card";
import { EmptyNote } from "@shared/ui/EmptyNote";
import { KeyValue, KeyValueRow } from "@shared/ui/KeyValue";
import { PageReadingContent, PageShell } from "@shared/ui/PageShell";
import * as PageState from "@shared/ui/PageState";
import { RouteBackLink } from "@shared/ui/RouteBackLink";
import { ArrowRight, ExternalLink } from "lucide-react";
import { useState } from "react";
import { Link, useLocation } from "react-router-dom";

import {
  type NewsDelivery,
  type NewsEventDetail,
  type NewsEventMember,
  type NewsEventReaction,
  type NewsQuote,
  type NewsReaction,
  type NewsSymbolNormalization,
  useNewsEventWithToken,
  useNewsItemRelatedEventsWithToken,
  useNewsQuotesWithToken,
} from "../../api/newsQueries";
import {
  absoluteTime,
  clockTime,
  displayAssetRefs,
  eventHeadline,
  optionalTime,
  timelineEndToEnd,
  validExternalUrl,
} from "../../model/newsLabels";
import { NewsAssetChips } from "../chrome/NewsAssetChips";
import { NewsTechnical } from "../chrome/NewsChrome";
import { NewsKindBadge } from "../chrome/NewsKindBadge";
import { NewsOutcomeBadge } from "../chrome/NewsOutcomeBadge";
import { NewsQuoteReadState } from "../chrome/NewsQuoteReadState";
import { NewsReactionValue } from "../chrome/NewsQuoteValue";

import { NewsEventPager } from "./NewsEventPager";
import {
  NewsProcessingState,
  NewsUpdateContent,
  NewsUpdateInference,
  NewsUpdateSources,
} from "./NewsEventUpdate";
import { NewsQuoteTable } from "./NewsQuoteTable";
import { NewsTimeline } from "./NewsTimeline";

import "./newsDetail.css";

export function NewsEventDetailPage({ eventId, token }: { eventId: string; token: string }) {
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
    <PageShell archetype="case" className="news-detail-shell" label="新闻事件详情">
      <header className="news-detail-toolbar">
        <RouteBackLink
          ariaLabel="返回新闻事件流"
          label="事件流"
          to={feedSearch ? `${newsPath()}?${feedSearch}` : newsPath()}
        />
        <NewsEventPager eventId={eventId} feedSearch={feedSearch} token={token} />
      </header>
      <PageReadingContent>
        {query.isLoading && !detail ? (
          <PageState.Loading label="正在读取事件详情" layout="panel" rows={5} />
        ) : null}
        {query.isError && !detail ? (
          <PageState.Error error={query.error} onRetry={() => void query.refetch()} />
        ) : null}
        {detail ? (
          <NewsQuoteReadState query={quotesQuery}>
            <EventDocument detail={detail} quotes={quotes} token={token} />
          </NewsQuoteReadState>
        ) : null}
      </PageReadingContent>
    </PageShell>
  );
}

function EventDocument({
  detail,
  quotes,
  token,
}: {
  detail: NewsEventDetail;
  quotes: Record<string, NewsQuote>;
  token: string;
}) {
  const { event, outcome } = detail;
  const update = detail.event_update ?? null;
  const headline = eventHeadline({
    leader_title: event.leader_title,
    update,
  });
  const url = validExternalUrl(event.leader_url);
  const assets = displayAssetRefs(event.grounded_assets ?? [], event.assets);
  const quoteList = assets.map((asset) => quotes[asset.symbol]).filter(Boolean);
  const steps = detail.timeline ?? [];
  const priorComparisonCount = update?.changes?.filter((change) => change.previous_ref).length ?? 0;
  return (
    <>
      <article className="news-detail-hero" data-update={update ? true : undefined}>
        <div className="news-detail-hero-top">
          {/* The conclusion and its one-line why, side by side: the chip is the verdict, the sentence is the
              server's reason for it. The chip does not repeat the reason inside itself. */}
          <span className="news-detail-hero-state">
            <NewsKindBadge kind={event.event_kind} />
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
        {update ? (
          <p className="news-detail-update-summary">
            本次提取 {update.claims.length} 条命题，来自 {update.sources?.length ?? 0} 个来源。
            {detail.processing?.notification?.plan ? (
              <span>
                通知：{detail.processing.notification.plan.action_zh} ·{" "}
                {detail.processing.notification.plan.reason_zh}
              </span>
            ) : null}
          </p>
        ) : null}
        {assets.length || update?.topics?.length ? (
          <div aria-label="事件判定" className="news-detail-verdict">
            {update?.topics?.length ? (
              <span className="news-detail-asset-group">
                <small>主题</small>
                {update.topics.map((topic) => (
                  <b key={topic.code}>{topic.label_zh}</b>
                ))}
              </span>
            ) : null}
            {assets.length && !update ? (
              <>
                <span aria-hidden className="news-detail-rule" />
                <NewsAssetChips assets={assets} quotes={quotes} />
              </>
            ) : null}
          </div>
        ) : null}

        {!update && quoteList.length ? <NewsQuoteTable quotes={quoteList} /> : null}

        {update ? (
          <div className="news-detail-update-foot">
            <span>原文 · {event.reporting_origin || "未知来源"}</span>
            <span>{update.claims.length} 条命题</span>
            <span>{priorComparisonCount} 项历史比较</span>
            <a href="#news-source">查看来源</a>
            <details>
              <summary>原始标题</summary>
              <p>{event.leader_title}</p>
              {url ? (
                <a href={url} rel="noreferrer" target="_blank">
                  打开原文 <ExternalLink aria-hidden />
                </a>
              ) : null}
            </details>
          </div>
        ) : (
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
        )}
      </article>

      {update ? (
        <>
          <nav aria-label="事件详情目录" className="news-detail-reading-nav">
            <a href="#news-content">本次内容</a>
            <a href="#news-source">来源证据</a>
            {detail.processing ? <a href="#news-processing">处理记录</a> : null}
            <a href="#news-market">行情观察</a>
          </nav>
          <div className="news-detail-reading-layout">
            <NewsUpdateContent update={update} />
            <NewsNotificationSummary detail={detail} />
            <NewsUpdateSources update={update} />
            <Card
              aria-label="当前行情"
              className="news-detail-current-market"
              hint="滚动报价"
              id="news-market"
              title="当前行情"
            >
              {assets.length ? <NewsAssetChips assets={assets} quotes={quotes} /> : null}
              <NewsQuoteTable compact quotes={quoteList} />
            </Card>
            <NewsUpdateInference update={update} />
            {detail.processing ? <NewsProcessingState processing={detail.processing} /> : null}
          </div>
        </>
      ) : null}
      {!update && detail.processing ? <NewsProcessingState processing={detail.processing} /> : null}

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

      <ReviewSummary detail={detail} />

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
          <Card
            aria-label="同类报道"
            hint={`${detail.members.length} 条，按到达时间`}
            title="同类报道"
          >
            <MemberList members={detail.members} />
          </Card>
        </div>
      </div>

      <RelatedItemEvents members={detail.members} currentEventId={event.event_id} token={token} />

      <TechnicalDetails detail={detail} />
    </>
  );
}

function NewsNotificationSummary({ detail }: { detail: NewsEventDetail }) {
  const plan = detail.processing?.notification?.plan;
  const decisions = plan?.claim_decisions ?? [];
  const positions = new Map(
    detail.event_update?.claims.map((claim, index) => [claim.ref, index + 1]),
  );
  return (
    <Card
      aria-label="通知判断"
      className="news-detail-notification-summary"
      title={detail.outcome.kind === "not_notified" ? "为什么未通知" : "通知判断"}
    >
      <p className="news-detail-notification-lead">
        {plan
          ? `${plan.action_zh} · ${plan.reason_zh}`
          : detail.outcome.reason_zh || detail.outcome.text_zh}
      </p>
      {decisions.length ? (
        <ol className="news-detail-notification-decisions">
          {decisions.slice(0, 3).map((row) => {
            const position = positions.get(row.claim_ref);
            return (
              <li key={row.claim_ref}>
                {position ? (
                  <a href={`#news-claim-${position}`}>命题 {String(position).padStart(2, "0")}</a>
                ) : (
                  <span>命题</span>
                )}
                <span>{row.reason_zh || row.reason}</span>
                <b>{row.decision_zh || row.decision}</b>
              </li>
            );
          })}
        </ol>
      ) : null}
      {decisions.length > 3 ? (
        <a className="news-detail-notification-more" href="#news-processing">
          查看全部 {decisions.length} 条逐条决定
        </a>
      ) : null}
      {detail.processing ? (
        <a className="news-detail-notification-more" href="#news-processing">
          查看处理记录
        </a>
      ) : null}
    </Card>
  );
}

const SHOULD_PUSH_LABELS: Record<string, string> = {
  must_push: "必须推送",
  should_push: "应该推送",
  should_hold: "应该保留",
  must_hold: "必须拦下",
  uncertain: "证据不足",
};

/** The latest current notification decision feedback for this Event. */
function ReviewSummary({ detail }: { detail: NewsEventDetail }) {
  const latest = detail.feedback.latest;
  return (
    <Card aria-label="人工复盘" hint={`${detail.feedback.feedback_n} 条判断`} title="人工复盘">
      {latest ? (
        <div className="news-detail-review-summary">
          <p>
            最新结论：<b>{SHOULD_PUSH_LABELS[latest.should_push] || latest.should_push}</b>
          </p>
          {latest.note ? <small>{latest.note}</small> : null}
          <small>
            {latest.reviewer} · {absoluteTime(latest.created_at_ms)}
          </small>
        </div>
      ) : (
        <EmptyNote>还没有当前通知决策的人工反馈。</EmptyNote>
      )}
    </Card>
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
  const primaryReactions = reactions.filter((reaction) => reaction.is_primary);
  const nonPrimaryCount = reactions.length - primaryReactions.length;
  if (!reactions.length && !aggregate) {
    return <EmptyNote>还没有可用的事件后反应。</EmptyNote>;
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
      {primaryReactions.length ? (
        <ul className="news-detail-reaction-list">
          {primaryReactions.map((reaction) => (
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
      {nonPrimaryCount ? (
        <p className="news-detail-reaction-caveat">
          已隐藏 {nonPrimaryCount} 个同名但非主标的的价格候选；它们不参与事件级评价。
        </p>
      ) : null}
    </div>
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
  const referrer = useRouteReferrer();
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
            {/* The collapsed identity is the one the token page is keyed on (#207 principle 9). */}
            <Link
              className="news-normalization-base"
              state={referrer}
              to={newsSymbolPath(group.base_symbol)}
            >
              <code>{group.base_symbol}</code>
            </Link>
          </li>
        ))}
      </ul>
    </Card>
  );
}

function MemberList({ members }: { members: NewsEventMember[] }) {
  if (!members.length) return <EmptyNote>没有成员记录。</EmptyNote>;
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

function RelatedItemEvents({
  members,
  currentEventId,
  token,
}: {
  members: NewsEventMember[];
  currentEventId: string;
  token: string;
}) {
  const [itemId, setItemId] = useState("");
  const [opened, setOpened] = useState(false);
  const query = useNewsItemRelatedEventsWithToken(token, itemId, opened);
  if (!members.length) return null;
  return (
    <Card
      aria-label="Item 关联事件"
      title="同一报道的关联事件"
      hint="按 Item 查询所有归属，包含非首条成员"
    >
      <label>
        选择报道
        <select
          value={itemId}
          onChange={(event) => {
            setItemId(event.target.value);
            setOpened(false);
          }}
        >
          <option value="">请选择</option>
          {members.map((member) => (
            <option key={member.item_id} value={member.item_id}>
              {member.title} · {member.item_id}
            </option>
          ))}
        </select>
      </label>
      <button disabled={!itemId} onClick={() => setOpened(true)} type="button">
        查看关联事件
      </button>
      {query.isPending && opened ? <p>正在读取关联事件…</p> : null}
      {query.isError ? <p role="alert">读取失败。请重试。</p> : null}
      {query.data ? (
        <>
          <p>共 {query.data.pages[0].total_events} 个 Event</p>
          <ol>
            {query.data.pages
              .flatMap((page) => page.events)
              .map((row) => (
                <li key={row.event_id}>
                  <Link to={newsEventPath(row.event_id)}>
                    {row.focus_fact_text || row.event_id}
                  </Link>
                  {row.event_id === currentEventId ? " · 当前 Event" : ""}
                  <p>
                    范围：{row.member_scopes.join("；") || "—"} · 成员类型：
                    {row.match_kinds.join("、") || "—"}
                  </p>
                  <p>
                    语义版本：{row.wanted_revision ?? "—"}/{row.done_revision ?? "—"} ·
                    {row.semantic_outcome ?? "待处理"} · 采纳：{row.adopted_content_revision ?? "—"}{" "}
                    · 决定：{row.notification_action ?? row.notification_state ?? "待处理"} · 意图：
                    {row.intent_state ?? "—"} · 已发送 {row.sent_count}
                  </p>
                </li>
              ))}
          </ol>
          {query.hasNextPage ? (
            <button
              disabled={query.isFetchingNextPage}
              onClick={() => void query.fetchNextPage()}
              type="button"
            >
              {query.isFetchingNextPage ? "读取中…" : "加载更多"}
            </button>
          ) : null}
        </>
      ) : null}
    </Card>
  );
}

function TechnicalDetails({ detail }: { detail: NewsEventDetail }) {
  const { event } = detail;
  return (
    <NewsTechnical summary="技术详情（事件 id、话题线与投递记录）">
      <section>
        <h4>事件</h4>
        <KeyValue>
          <KeyValueRow k="event_id" v={event.event_id} />
          <KeyValueRow k="storyline_key" v={event.storyline_key} />
          <KeyValueRow k="admission" v={event.admission} />
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
