import { newsEventPath, newsPath, newsSymbolPath } from "@shared/routing/paths";
import { useRouteReferrer } from "@shared/routing/routeReferrer";
import { ActionButton } from "@shared/ui/ActionButton";
import { EmptyNote } from "@shared/ui/EmptyNote";
import { KeyValue, KeyValueRow } from "@shared/ui/KeyValue";
import { PageReadingContent, PageShell } from "@shared/ui/PageShell";
import * as PageState from "@shared/ui/PageState";
import { RouteBackLink } from "@shared/ui/RouteBackLink";
import { ArrowRight, Copy, ExternalLink } from "lucide-react";
import { useLayoutEffect, useRef, useState } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";

import {
  type NewsDelivery,
  type NewsEventDetail,
  type NewsEventMember,
  type NewsSymbolNormalization,
  newsAssetKey,
  useNewsEventWithToken,
  useNewsItemRelatedEventsWithToken,
  useNewsQuotesWithToken,
} from "../../api/newsQueries";
import {
  eventReader,
  eventTiming,
  seconds,
  sourceDisplayName,
  sourcePlatform,
} from "../../model/eventReader";
import {
  absoluteTime,
  clockTime,
  optionalTime,
  validExternalUrl,
  outcomeReason,
} from "../../model/newsLabels";
import { useNewsDetailStart } from "../../state/useNewsReadingPosition";
import { NewsAssetChips } from "../chrome/NewsAssetChips";
import { NewsTechnical } from "../chrome/NewsChrome";
import { NewsKindBadge } from "../chrome/NewsKindBadge";
import { NewsOutcomeBadge } from "../chrome/NewsOutcomeBadge";
import { NewsQuoteReadState } from "../chrome/NewsQuoteReadState";

import { NewsEventPager } from "./NewsEventPager";
import {
  NewsReaderDelivery,
  NewsReaderFacts,
  NewsReaderSources,
  NewsReaderStory,
  NewsReaderTiming,
} from "./NewsEventReader";
import {
  NewsClaimRecords,
  NewsProcessingState,
  NewsUpdateInference,
  type NewsDetailNavigate,
} from "./NewsEventUpdate";
import { NewsQuoteTable } from "./NewsQuoteTable";
import { NewsTimeline } from "./NewsTimeline";
import "./newsDetail.css";
const TIME_ZONE_FORMATTER = new Intl.DateTimeFormat("en", { timeZoneName: "shortOffset" });
function zonedTime(value: number) {
  const zone = TIME_ZONE_FORMATTER.formatToParts(new Date(value)).find(
    (part) => part.type === "timeZoneName",
  )?.value;
  return `${absoluteTime(value)} ${zone ?? ""}`.trim();
}

export function NewsEventDetailPage({ eventId, token }: { eventId: string; token: string }) {
  const query = useNewsEventWithToken(token, eventId);
  const detail = query.data;
  useNewsDetailStart(`event:${eventId}`, !detail);
  const feedSearch = (useLocation().state as { feedSearch?: string } | null)?.feedSearch ?? null;
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
          <PageState.Stale
            failedRefresh={
              query.isError
                ? `事件详情刷新失败 · 显示上次成功读取的内容（${absoluteTime(query.dataUpdatedAt)}）`
                : undefined
            }
            onRetry={() => void query.refetch()}
            updating={query.isFetching && !query.isError}
          >
            <EventDocument detail={detail} key={eventId} token={token} />
          </PageState.Stale>
        ) : null}
      </PageReadingContent>
    </PageShell>
  );
}

function EventDocument({ detail, token }: { detail: NewsEventDetail; token: string }) {
  const { event, outcome } = detail;
  useNewsDetailStart(`event:${event.event_id}`);
  const update = detail.event_update;
  const reader = eventReader(detail);
  const assets = event.assets ?? [];
  const timing = eventTiming(detail);
  const quotesQuery = useNewsQuotesWithToken(token, assets);
  const quotes = new Map(
    (quotesQuery.data?.quotes ?? []).map((quote) => [
      newsAssetKey(quote.market_type, quote.requested_symbol),
      quote,
    ]),
  );
  const quoteList = assets.flatMap((asset) => {
    const quote = quotes.get(newsAssetKey(asset.market_type, asset.symbol));
    return quote ? [quote] : [];
  });
  const location = useLocation();
  const navigate = useNavigate();
  const root = useRef<HTMLDivElement>(null);
  const params = new URLSearchParams(location.search);
  const focus = params.get("focus");
  const section = params.get("tab");
  const [copyState, setCopyState] = useState<"idle" | "copied" | "failed">("idle");
  const onNavigate: NewsDetailNavigate = (target) => {
    const next = new URLSearchParams(location.search);
    next.delete("tab");
    next.set("focus", target);
    navigate(
      { pathname: location.pathname, search: `?${next.toString()}`, hash: "" },
      { state: location.state, preventScrollReset: true },
    );
  };
  useLayoutEffect(() => {
    // Older shared tab URLs now locate the corresponding section of the one continuous document.
    const id =
      focus ??
      (
        {
          content: "news-content",
          source: "news-source",
          market: "news-market",
          processing: "news-processing",
        } as Record<string, string>
      )[section ?? ""];
    const target = id
      ? Array.from(root.current?.querySelectorAll<HTMLElement>("[id]") ?? []).find(
          (node) => node.id === id,
        )
      : undefined;
    if (!target) return;
    let node: HTMLElement | null = target;
    while (node && node !== root.current) {
      if (node instanceof HTMLDetailsElement) node.open = true;
      node = node.parentElement;
    }
    target.focus({ preventScroll: true });
    target.scrollIntoView({ block: "start", inline: "nearest" });
  }, [focus, section, location.key]);
  async function copyEventLink() {
    try {
      await navigator.clipboard.writeText(
        `${window.location.origin}${newsEventPath(event.event_id)}${location.search}`,
      );
      setCopyState("copied");
    } catch {
      setCopyState("failed");
    }
  }
  const semantic = detail.processing?.semantic;
  const latestFailed = semantic?.state === "failed";
  const hasNewInput = !!update && !!semantic && semantic.wanted_revision > update.input_revision;
  const source = update?.sources?.[0]?.source;
  const url = validExternalUrl(source?.url || event.leader_url);
  const sentCount = reader.facts.filter((fact) => fact.sent).length;
  const headline = reader.latestSent?.headline_zh || event.leader_title;
  const notification = detail.processing?.notification;
  const original = update?.duplicates?.[0];
  const originalAt = original?.received_at_ms ?? original?.first_available_at_ms;
  return (
    <div className="news-detail-document" ref={root}>
      <article className="news-detail-hero">
        <div className="news-detail-hero-top">
          <span className="news-detail-hero-state">
            <NewsOutcomeBadge outcome={outcome} size="lg" variant="chip" />
            {reader.latestSent?.settled_at_ms != null ? (
              <time>
                {zonedTime(reader.latestSent.settled_at_ms)} 送达
                {timing.elapsed != null ? ` · 收到后 ${seconds(timing.elapsed)}` : ""}
              </time>
            ) : null}
            <NewsKindBadge kind={event.event_kind} />
          </span>
          <ActionButton onClick={() => void copyEventLink()}>
            <Copy aria-hidden />
            复制事件链接
          </ActionButton>
        </div>
        <h1 className="news-detail-headline">{headline}</h1>
        <p className="news-detail-update-summary">
          {update
            ? `${reader.facts.length} 件事 · ${
                sentCount
                  ? `推送了 ${sentCount} 件${reader.latestSent?.settled_at_ms != null ? ` · ${clockTime(reader.latestSent.settled_at_ms)} 送达` : ""}`
                  : outcome.kind === "duplicate"
                    ? `与${originalAt != null ? ` ${clockTime(originalAt)} 的` : "更早的"}事件内容相同，不单独推送`
                    : outcome.kind === "not_notified"
                      ? `未推送${notification?.carried && notification.decided_at_ms != null ? ` · ${clockTime(notification.decided_at_ms)} 判断` : ""}`
                      : "本次尚无确认送达"
              }${
                detail.processing?.notification?.carried
                  ? `${sentCount ? " · " : "，"}${
                      detail.processing.notification.added_sources?.length
                        ? `之后新增 ${detail.processing.notification.added_sources.length} 个来源${sentCount ? "，内容未变" : ""}`
                        : "之后仅非通知变化"
                    }`
                  : ""
              }`
            : outcomeReason(outcome)}
        </p>
        <div className="news-reader-meta">
          <b>
            {sourceDisplayName(source, event.reporting_origin || "来源未记录")}
            {sourcePlatform(source?.url || event.leader_url)
              ? ` · ${sourcePlatform(source?.url || event.leader_url)}`
              : ""}
          </b>
          <time
            className="news-detail-hero-time"
            dateTime={new Date(
              source?.published_at_ms ?? event.published_at_ms ?? event.opened_at_ms,
            ).toISOString()}
          >
            {zonedTime(source?.published_at_ms ?? event.published_at_ms ?? event.opened_at_ms)} 发布
          </time>
          {url ? (
            <a href={url} rel="noreferrer" target="_blank">
              原文 <ExternalLink aria-hidden />
            </a>
          ) : null}
          {assets.length ? <NewsAssetChips assets={assets} /> : null}
          {event.ingest_mode === "recovery" ? <span>断线补抄进入</span> : null}
        </div>
        {latestFailed || hasNewInput ? (
          <p className="news-detail-work-notice" role={latestFailed ? "alert" : "status"}>
            {latestFailed ? "最新材料处理失败" : "最新材料尚未采用"}
            {update ? ` · 保留已采用的第 ${update.input_revision} 版内容` : " · 暂无已采用内容"}
          </p>
        ) : null}
        {detail.processing?.update_error_code ||
        detail.processing?.notification?.plan_error_code ? (
          <p className="news-detail-work-notice" role="alert">
            部分已记录内容暂不可读，请查看工程细节中的错误记录。
          </p>
        ) : null}
        {copyState !== "idle" ? (
          <span className="news-detail-copy-status" role="status">
            {copyState === "copied" ? "事件链接已复制" : "复制失败，请复制浏览器地址"}
          </span>
        ) : null}
      </article>
      <div className="news-reader-layout">
        <div className="news-reader-main">
          <NewsReaderDelivery detail={detail} onNavigate={onNavigate} />
          <NewsReaderFacts detail={detail} onNavigate={onNavigate} />
          <NewsReaderSources detail={detail} onNavigate={onNavigate} />
        </div>
        <div className="news-reader-context" aria-label="故事与用时" role="group">
          <NewsReaderStory detail={detail} />
          <NewsReaderTiming detail={detail} />
        </div>
        <details
          className="news-reader-section news-reader-engineering"
          id="news-processing"
          tabIndex={-1}
        >
          <summary>工程细节（处理记录、模型分数、召回）</summary>
          <NewsProcessingState
            onNavigate={onNavigate}
            processing={detail.processing}
            update={update}
          />
          {update ? (
            <>
              <details className="news-detail-record">
                <summary>事实字段、来源关系与历史比较</summary>
                <NewsClaimRecords onNavigate={onNavigate} update={update} />
              </details>
              <NewsUpdateInference update={update} />
            </>
          ) : null}
          <details className="news-detail-record" id="timeline-record" tabIndex={-1}>
            <summary>处理时间线</summary>
            <NewsTimeline steps={detail.timeline ?? []} />
          </details>
          <details className="news-detail-record" id="receipt-record" tabIndex={-1}>
            <summary>投递回执 · {detail.deliveries.length} 条</summary>
            {detail.deliveries.length ? (
              detail.deliveries.map((delivery, index) => (
                <DeliveryRecord delivery={delivery} key={`${delivery.intent_id}-${index}`} />
              ))
            ) : (
              <EmptyNote>暂无投递回执。</EmptyNote>
            )}
          </details>
          <details className="news-detail-record" id="member-record" tabIndex={-1}>
            <summary>同类报道 · {detail.members.length} 条</summary>
            <MemberList members={detail.members} />
          </details>
          <TechnicalDetails detail={detail} token={token} />
        </details>
        <details className="news-reader-section" id="news-market" tabIndex={-1}>
          <summary>当前行情 · 滚动报价</summary>
          <p className="news-reader-note">当前报价与滚动 24H 变化，不是这次事件的回填收益。</p>
          <NewsAssetChips assets={assets} />
          <NewsQuoteReadState query={quotesQuery}>
            {quotesQuery.isLoading || (quotesQuery.isError && !quotesQuery.data) ? null : (
              <NewsQuoteTable compact quotes={quoteList} />
            )}
          </NewsQuoteReadState>
        </details>
      </div>
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
    <section aria-label="符号归一" className="news-detail-normalization">
      <h4>符号归一</h4>
      <p className="news-detail-panel-note">节流键按 base_symbol 分桶，不按合约。</p>
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
    </section>
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
    <section
      className="news-detail-related"
      aria-label="Item 关联事件"
      id="related-items"
      tabIndex={-1}
    >
      <h4>同一报道的关联事件</h4>
      <p className="news-detail-panel-note">按 Item 查询所有归属，包含非首条成员。</p>
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
      <ActionButton disabled={!itemId} onClick={() => setOpened(true)}>
        查看关联事件
      </ActionButton>
      {query.isPending && opened ? <p>正在读取关联事件…</p> : null}
      {query.isError ? (
        <PageState.Error error={query.error} onRetry={() => void query.refetch()} />
      ) : null}
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
    </section>
  );
}

function TechnicalDetails({ detail, token }: { detail: NewsEventDetail; token: string }) {
  const { event } = detail;
  return (
    <NewsTechnical summary="技术详情">
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
          <KeyValueRow k="content_revision" v={detail.event_update?.content_revision ?? "—"} />
          <KeyValueRow k="input_revision" v={String(detail.event_update?.input_revision ?? "—")} />
          <KeyValueRow k="headline_source" v={detail.event_update?.headline_source ?? "—"} />
          <KeyValueRow
            k="topics"
            v={(detail.event_update?.topics ?? []).map((topic) => topic.code).join(", ") || "—"}
          />
        </KeyValue>
      </section>
      {detail.event_update?.sources?.some((source) => source.relations?.length) ? (
        <NewsTechnical summary="来源关系原始记录">
          <pre className="news-json">
            {JSON.stringify(
              detail.event_update.sources?.map((source) => ({
                evidence_ref: source.evidence_ref,
                relations: source.relations,
              })),
              null,
              2,
            )}
          </pre>
        </NewsTechnical>
      ) : null}
      <SymbolNormalization groups={detail.normalization ?? []} />
      <RelatedItemEvents members={detail.members} currentEventId={event.event_id} token={token} />
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
