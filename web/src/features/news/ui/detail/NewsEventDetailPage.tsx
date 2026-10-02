import { newsEventPath, newsPath, newsSymbolPath } from "@shared/routing/paths";
import { useRouteReferrer } from "@shared/routing/routeReferrer";
import { ActionButton } from "@shared/ui/ActionButton";
import { Card } from "@shared/ui/Card";
import { EmptyNote } from "@shared/ui/EmptyNote";
import { KeyValue, KeyValueRow } from "@shared/ui/KeyValue";
import { PageReadingContent, PageShell } from "@shared/ui/PageShell";
import * as PageState from "@shared/ui/PageState";
import { RouteBackLink } from "@shared/ui/RouteBackLink";
import { ArrowRight, Copy, ExternalLink, FileText } from "lucide-react";
import { useLayoutEffect, useRef, useState, type KeyboardEvent } from "react";
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
  absoluteTime,
  clockTime,
  eventHeadline,
  optionalTime,
  timelineEndToEnd,
  validExternalUrl,
} from "../../model/newsLabels";
import { useNewsDetailStart } from "../../state/useNewsReadingPosition";
import { NewsAssetChips } from "../chrome/NewsAssetChips";
import { NewsTechnical } from "../chrome/NewsChrome";
import { NewsKindBadge } from "../chrome/NewsKindBadge";
import { NewsOutcomeBadge } from "../chrome/NewsOutcomeBadge";
import { NewsQuoteReadState } from "../chrome/NewsQuoteReadState";

import { NewsEventPager } from "./NewsEventPager";
import {
  type NewsDetailNavigate,
  NewsProcessingState,
  NewsUpdateContent,
  NewsUpdateInference,
  NewsUpdateSources,
} from "./NewsEventUpdate";
import { NewsQuoteTable } from "./NewsQuoteTable";
import { NewsTimeline } from "./NewsTimeline";

import "./newsDetail.css";

const DETAIL_TABS = [
  { id: "content", label: "事件内容" },
  { id: "source", label: "来源证据" },
  { id: "market", label: "当前行情" },
  { id: "processing", label: "处理记录" },
] as const;

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
  // Run before the query focus effect so a shared target can still bring its record into view.
  useNewsDetailStart(`event:${event.event_id}`);
  const update = detail.event_update;
  const assets = event.assets ?? [];
  const quotesQuery = useNewsQuotesWithToken(token, assets);
  const quotes = Object.fromEntries(
    (quotesQuery.data?.quotes ?? []).map((quote) => [
      newsAssetKey(quote.market_type, quote.requested_symbol),
      quote,
    ]),
  );
  const quoteList = assets
    .map((asset) => quotes[newsAssetKey(asset.market_type, asset.symbol)])
    .filter(Boolean);
  const location = useLocation();
  const navigate = useNavigate();
  const params = new URLSearchParams(location.search);
  const requestedTab = params.get("tab");
  const tab = DETAIL_TABS.find((item) => item.id === requestedTab)?.id ?? "content";
  const focus = params.get("focus");
  const contentSlot = useRef<HTMLDivElement>(null);
  const tabs = useRef<HTMLDivElement>(null);
  const [copyState, setCopyState] = useState<"idle" | "copied" | "failed">("idle");

  // Reserve the existing reading height before hiding a panel. A shorter panel must not make the
  // browser clamp the reader's scroll position, including when returning through browser history.
  function reserveHeight() {
    const slot = contentSlot.current;
    if (slot) slot.style.minHeight = `${slot.getBoundingClientRect().height}px`;
  }
  const onNavigate: NewsDetailNavigate = (nextTab, target) => {
    reserveHeight();
    const next = new URLSearchParams(location.search);
    next.set("tab", nextTab);
    if (target) next.set("focus", target);
    else next.delete("focus");
    navigate(
      { pathname: location.pathname, search: `?${next.toString()}`, hash: "" },
      { state: location.state, preventScrollReset: true },
    );
  };

  useLayoutEffect(() => {
    const slot = contentSlot.current;
    if (!slot) return;
    const panel = slot.querySelector<HTMLElement>(`#news-${tab}`);
    const target = focus
      ? Array.from(panel?.querySelectorAll<HTMLElement>("[id]") ?? []).find(
          (node) => node.id === focus,
        )
      : undefined;
    if (target) {
      let node: HTMLElement | null = target;
      while (node && node !== panel) {
        if (node instanceof HTMLDetailsElement) node.open = true;
        node = node.parentElement;
      }
      target.focus({ preventScroll: true });
      const viewport = target.closest(".center-column")?.getBoundingClientRect();
      const top = viewport?.top ?? 0;
      const bottom = viewport?.bottom ?? window.innerHeight;
      const bounds = target.getBoundingClientRect();
      const oversized = bounds.height > bottom - top;
      if (bounds.top < top || (oversized ? bounds.top >= bottom : bounds.bottom > bottom)) {
        target.scrollIntoView({ block: oversized ? "start" : "nearest", inline: "nearest" });
      }
    } else if (
      document.activeElement?.closest('[role="tabpanel"][hidden]') ||
      (document.activeElement && tabs.current?.contains(document.activeElement))
    ) {
      tabs.current?.querySelector<HTMLButtonElement>(`#tab-${tab}`)?.focus({ preventScroll: true });
    }
    reserveHeight();
  }, [tab, focus, location.key]);

  useLayoutEffect(() => {
    const slot = contentSlot.current;
    // Capture growth while the panel is still visible. A POP navigation bypasses onNavigate,
    // and a layout-effect cleanup can run after React has hidden the old panel.
    const observer =
      typeof ResizeObserver === "undefined" ? null : new ResizeObserver(reserveHeight);
    if (slot) {
      observer?.observe(slot);
      slot.addEventListener("toggle", reserveHeight, true);
    }
    const clearHeight = () => {
      if (contentSlot.current) contentSlot.current.style.minHeight = "";
    };
    window.addEventListener("resize", clearHeight);
    return () => {
      window.removeEventListener("resize", clearHeight);
      slot?.removeEventListener("toggle", reserveHeight, true);
      observer?.disconnect();
    };
  }, []);

  function onTabKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    const current = DETAIL_TABS.findIndex((item) => item.id === tab);
    let index: number;
    switch (event.key) {
      case "ArrowRight":
        index = (current + 1) % DETAIL_TABS.length;
        break;
      case "ArrowLeft":
        index = (current + DETAIL_TABS.length - 1) % DETAIL_TABS.length;
        break;
      case "Home":
        index = 0;
        break;
      case "End":
        index = DETAIL_TABS.length - 1;
        break;
      default:
        return;
    }
    event.preventDefault();
    onNavigate(DETAIL_TABS[index].id);
    tabs.current
      ?.querySelector<HTMLButtonElement>(`#tab-${DETAIL_TABS[index].id}`)
      ?.focus({ preventScroll: true });
  }

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
  return (
    <div className="news-detail-document">
      <article className="news-detail-hero">
        <div className="news-detail-hero-top">
          <span className="news-detail-hero-state">
            <NewsOutcomeBadge outcome={outcome} size="lg" variant="chip" />
            <NewsKindBadge kind={event.event_kind} />
            {update?.topics?.map((topic) => (
              <span key={topic.code}>{topic.label_zh}</span>
            ))}
          </span>
          <time
            className="news-detail-hero-time"
            dateTime={new Date(event.published_at_ms ?? event.opened_at_ms).toISOString()}
          >
            {zonedTime(event.published_at_ms ?? event.opened_at_ms)}
          </time>
        </div>
        <h1 className="news-detail-headline">
          {eventHeadline({ leader_title: event.leader_title, update })}
        </h1>
        <p className="news-detail-update-summary">
          {update?.claims[0]?.statement || event.leader_description || event.leader_title}
        </p>
        {latestFailed || hasNewInput ? (
          <p className="news-detail-work-notice" role={latestFailed ? "alert" : "status"}>
            {latestFailed ? "最新材料处理失败" : "最新材料尚未采用"}
            {update ? ` · 保留已采用的第 ${update.input_revision} 版内容` : " · 暂无已采用内容"}
            {semantic?.last_error_code ? ` · ${semantic.last_error_code}` : ""}
          </p>
        ) : null}
        <div className="news-detail-hero-footer">
          <div className="news-detail-hero-facts">
            {assets.length ? <NewsAssetChips assets={assets} /> : null}
            <span>
              {update
                ? `${update.sources?.length ?? 0} 份来源材料 · ${update.claims.length} 条提取命题`
                : `${detail.members.length} 条报道 · 暂无已采用命题`}
            </span>
          </div>
          <div className="news-detail-hero-actions">
            <ActionButton onClick={() => onNavigate("source")} variant="primary">
              <FileText aria-hidden /> 查看来源证据
            </ActionButton>
            <ActionButton onClick={() => void copyEventLink()}>
              <Copy aria-hidden /> 复制事件链接
            </ActionButton>
          </div>
          {copyState !== "idle" ? (
            <span className="news-detail-copy-status" role="status">
              {copyState === "copied" ? "事件链接已复制" : "复制失败，请复制浏览器地址"}
            </span>
          ) : null}
        </div>
      </article>

      <div className="news-detail-reading-layout">
        <section aria-label="事件阅读" className="news-detail-main">
          <div
            aria-label="事件详情"
            className="news-detail-tabs"
            onKeyDown={onTabKeyDown}
            ref={tabs}
            role="tablist"
            tabIndex={-1}
          >
            {DETAIL_TABS.map((item) => (
              <button
                aria-controls={`news-${item.id}`}
                aria-selected={tab === item.id}
                id={`tab-${item.id}`}
                key={item.id}
                onClick={() => onNavigate(item.id)}
                role="tab"
                tabIndex={tab === item.id ? 0 : -1}
                type="button"
              >
                {item.label}
                {item.id === "content" ? (
                  <small aria-hidden>{String(update?.claims.length ?? 0).padStart(2, "0")}</small>
                ) : null}
                {item.id === "source" ? (
                  <small aria-hidden>
                    {String(
                      update ? (update.sources?.length ?? 0) : detail.members.length,
                    ).padStart(2, "0")}
                  </small>
                ) : null}
              </button>
            ))}
          </div>
          <div className="news-detail-tab-content" ref={contentSlot}>
            <div
              aria-labelledby="tab-content"
              hidden={tab !== "content"}
              id="news-content"
              role="tabpanel"
              tabIndex={0}
            >
              {update ? (
                <>
                  <NewsUpdateContent onNavigate={onNavigate} update={update} />
                  <NewsUpdateInference update={update} />
                </>
              ) : (
                <section className="news-detail-unadopted">
                  <h2>事件内容</h2>
                  <p className="news-detail-panel-note">暂无已采用内容，以下为来源记录。</p>
                  <h3>{event.leader_title}</h3>
                  {event.leader_description ? <p>{event.leader_description}</p> : null}
                  <ActionButton onClick={() => onNavigate("source")}>查看来源证据</ActionButton>
                </section>
              )}
            </div>
            <div
              aria-labelledby="tab-source"
              hidden={tab !== "source"}
              id="news-source"
              role="tabpanel"
              tabIndex={0}
            >
              {update ? (
                <NewsUpdateSources onNavigate={onNavigate} update={update} />
              ) : (
                <section>
                  <h2>来源证据</h2>
                  <p className="news-detail-panel-note">尚无采用后的来源关系。</p>
                </section>
              )}
              <details className="news-detail-record" id="member-record" tabIndex={-1}>
                <summary>同类报道 · {detail.members.length} 条</summary>
                <MemberList members={detail.members} />
              </details>
              <details className="news-detail-record">
                <summary>原始标题</summary>
                <p>{event.leader_title}</p>
                {validExternalUrl(event.leader_url) ? (
                  <a href={validExternalUrl(event.leader_url)!} rel="noreferrer" target="_blank">
                    打开原文 <ExternalLink aria-hidden />
                  </a>
                ) : null}
              </details>
            </div>
            <div
              aria-labelledby="tab-market"
              hidden={tab !== "market"}
              id="news-market"
              role="tabpanel"
              tabIndex={0}
            >
              <div className="news-detail-panel-heading">
                <h2>当前行情</h2>
                <small>滚动报价</small>
              </div>
              <p className="news-detail-panel-note">
                当前报价与滚动 24H 变化，不是这次事件的回填收益。
              </p>
              {assets.length ? <NewsAssetChips assets={assets} /> : null}
              <NewsQuoteReadState query={quotesQuery}>
                {quotesQuery.isLoading || (quotesQuery.isError && !quotesQuery.data) ? null : (
                  <NewsQuoteTable compact quotes={quoteList} />
                )}
              </NewsQuoteReadState>
            </div>
            <div
              aria-labelledby="tab-processing"
              hidden={tab !== "processing"}
              id="news-processing"
              role="tabpanel"
              tabIndex={0}
            >
              <NewsProcessingState
                onNavigate={onNavigate}
                processing={detail.processing}
                update={update}
              />
              <details className="news-detail-record" id="timeline-record" tabIndex={-1}>
                <summary>处理时间线 · {timelineEndToEnd(detail.timeline ?? [])}</summary>
                <p className="news-detail-panel-note">
                  按已记录步骤展示；历史送达不代表最新材料已经处理或发送。
                </p>
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
            </div>
          </div>
        </section>
        <NewsNotificationSummary detail={detail} onNavigate={onNavigate} />
      </div>
      <TechnicalDetails detail={detail} token={token} />
    </div>
  );
}

function NewsNotificationSummary({
  detail,
  onNavigate,
}: {
  detail: NewsEventDetail;
  onNavigate: NewsDetailNavigate;
}) {
  const notification = detail.processing?.notification;
  const plan = notification?.plan;
  const decisions = plan?.claim_decisions ?? [];
  const positions = new Map(
    detail.event_update?.claims.map((claim, index) => [claim.ref, index + 1]),
  );
  const sent = detail.processing?.intents?.filter((intent) => intent.state === "sent") ?? [];
  const latestSent = sent.reduce<number | null>((latest, intent) => {
    const stamp = intent.settled_at_ms;
    return stamp != null && (latest == null || stamp > latest) ? stamp : latest;
  }, null);
  const receipt = detail.reader_receipt;
  const unfinishedStates = Array.from(
    new Set(
      (detail.processing?.intents ?? [])
        .filter((intent) => intent.state !== "sent")
        .map(
          (intent) =>
            `${intent.state_zh || intent.state}${intent.error_code ? ` · ${intent.error_code}` : ""}`,
        ),
    ),
  );
  const currentRevision = detail.event_update?.content_revision;
  const historicalSent =
    currentRevision != null &&
    sent.some(
      (intent) => intent.content_revision != null && intent.content_revision !== currentRevision,
    );
  return (
    <Card aria-label="通知与送达" className="news-detail-notification-summary" title="通知与送达">
      <p className="news-detail-notification-lead">
        {plan ? plan.reason_zh || plan.reason : detail.outcome.reason_zh || detail.outcome.text_zh}
      </p>
      <p className="news-detail-panel-note">
        {plan ? `通知决定：${plan.action_zh}` : `通知：${notification?.state_zh || "暂无决定"}`}
      </p>
      {decisions.length ? (
        <ol className="news-detail-notification-decisions">
          {decisions.slice(0, 3).map((row) => {
            const position = positions.get(row.claim_ref);
            return (
              <li key={row.claim_ref}>
                {position ? (
                  <button
                    onClick={() => onNavigate("content", `news-claim-${position}`)}
                    type="button"
                  >
                    命题 {String(position).padStart(2, "0")}
                  </button>
                ) : (
                  <span>历史命题</span>
                )}
                <b>{row.decision_zh || row.decision}</b>
              </li>
            );
          })}
        </ol>
      ) : null}
      <div className="news-detail-delivery-summary">
        <span>实际送达</span>
        <b>
          {sent.length
            ? `已送达 · ${sent.length} 条记录`
            : receipt.state === "received"
              ? "已有送达回执"
              : "暂无成功送达记录"}
        </b>
        {latestSent != null ? (
          <time dateTime={new Date(latestSent).toISOString()}>{zonedTime(latestSent)}</time>
        ) : null}
        {historicalSent ? <p>包含历史版本送达，当前内容是否发出请核对发送正文。</p> : null}
        {unfinishedStates.length ? <p>其他发送工作：{unfinishedStates.join("；")}</p> : null}
        <button
          onClick={() =>
            onNavigate(
              "processing",
              detail.processing?.intents?.length ? "delivery-record" : "receipt-record",
            )
          }
          type="button"
        >
          查看发送正文与记录 →
        </button>
        <button onClick={() => onNavigate("processing", "decision-record")} type="button">
          查看{decisions.length > 3 ? `全部 ${decisions.length} 条` : "逐条"}通知理由 →
        </button>
      </div>
    </Card>
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
    <section className="news-detail-related" aria-label="Item 关联事件">
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
        </KeyValue>
      </section>
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
