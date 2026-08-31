import { TradingCaseBadge } from "@features/trading";
import { newsPath, newsSymbolPath } from "@shared/routing/paths";
import { useRouteReferrer } from "@shared/routing/routeReferrer";
import { Card } from "@shared/ui/Card";
import { FactGrid } from "@shared/ui/FactGrid";
import { KeyValue, KeyValueRow } from "@shared/ui/KeyValue";
import * as PageState from "@shared/ui/PageState";
import { RouteBackLink } from "@shared/ui/RouteBackLink";
import { ArrowRight, ExternalLink } from "lucide-react";
import { Link, useLocation } from "react-router-dom";

import {
  type NewsDelivery,
  type NewsEventDetail,
  type NewsEventMember,
  type NewsEventReaction,
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
  optionalTime,
  timelineEndToEnd,
  validExternalUrl,
} from "../../model/newsLabels";
import { NewsAssetChips } from "../chrome/NewsAssetChips";
import { NewsEmptyNote, NewsPageShell, NewsTechnical } from "../chrome/NewsChrome";
import { NewsDirectionChip } from "../chrome/NewsDirectionChip";
import { NewsKindBadge } from "../chrome/NewsKindBadge";
import { NewsOutcomeBadge } from "../chrome/NewsOutcomeBadge";
import { NewsQuoteReadState } from "../chrome/NewsQuoteReadState";
import { NewsReactionValue } from "../chrome/NewsQuoteValue";

import { NewsEventPager } from "./NewsEventPager";
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
      {detail ? (
        <NewsQuoteReadState query={quotesQuery}>
          <EventDocument detail={detail} quotes={quotes} token={token} />
        </NewsQuoteReadState>
      ) : null}
    </NewsPageShell>
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
  const { event, outcome, triage } = detail;
  const headline = triage?.headline_zh?.trim() || event.leader_title;
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
            <NewsKindBadge kind={event.event_kind} />
            <NewsOutcomeBadge outcome={outcome} size="lg" variant="chip" />
            {outcome.reason_zh ? <span>{outcome.reason_zh}</span> : null}
            {/*
             * Did the Signal lane admit this? It renders nothing at all for a model-lane Event,
             * because that question genuinely cannot be asked there — only the deterministic lane's source
             * key is reconstructible from an `event_id`, and a 未成案 chip would report a refusal that never
             * happened.
             */}
            <TradingCaseBadge
              eventId={event.event_id}
              lane={event.admission === "telemetry_deterministic" ? "oi" : "news"}
              token={token}
            />
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
        {triage || assets.length ? (
          <div aria-label="事件判定" className="news-detail-verdict">
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
        {triage ? <VerdictAssets triage={triage} /> : null}

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

      <TechnicalDetails detail={detail} />
    </>
  );
}

const SHOULD_PUSH_LABELS: Record<string, string> = {
  must_push: "必须推送",
  should_push: "应该推送",
  should_hold: "应该保留",
  must_hold: "必须拦下",
  uncertain: "证据不足",
};

/**
 * Whether a human has judged this Event, and what they concluded.
 *
 * The ReviewDesk console is gone (#256) and the judgments are not: `tracefold news review submit` still
 * appends them and `/api/news/events/{event_id}` still serves the accepted one. What went with the page is
 * the link into it — this card reports the judgment, it is no longer a door to making one.
 */
function ReviewSummary({ detail }: { detail: NewsEventDetail }) {
  const accepted = detail.review.accepted;
  return (
    <Card
      aria-label="人工复盘"
      hint={`${detail.review.judgment_n} 条不可变判断${detail.review.uncertain ? " · 尚有分歧" : ""}`}
      title="人工复盘"
    >
      {accepted ? (
        <div className="news-detail-review-summary">
          <p>
            最新接受结论：
            <b>{SHOULD_PUSH_LABELS[accepted.should_push || "uncertain"] || accepted.should_push}</b>
          </p>
          {accepted.first_bad_owner ? <small>第一处错误：{accepted.first_bad_owner}</small> : null}
          {accepted.note ? <small>{accepted.note}</small> : null}
        </div>
      ) : (
        <NewsEmptyNote>还没有经过接受的人工复盘；这里不会用 1H 涨跌代替判断。</NewsEmptyNote>
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
 * The rest of the current judgment, in the server's own words. A cell is omitted rather than rendered as a
 * dash when the server has nothing for it, so a macro Event with no assets does not show a row of dashes.
 */
function VerdictFacts({ triage }: { triage: NewsTriageSummary }) {
  const taxonomy = triage.taxonomy;
  return (
    <FactGrid
      className="news-detail-fact-grid"
      facts={[
        { label: "事件族", value: taxonomy?.event_family_zh ?? "" },
        { label: "变化状态", value: taxonomy?.change_state_zh ?? "" },
        { label: "来源权威", value: taxonomy?.source_authority_zh ?? "" },
        { label: "断言状态", value: taxonomy?.assertion_status_zh ?? "" },
        { label: "主题", value: taxonomy?.subject_labels_zh?.join("、") ?? "" },
        { label: "范围", value: triage.scope_zh },
        // Confidence used to sit beside the direction, where it competed with the one number that matters
        // there. It is a judgment detail like the rest, so it reads as one (#87).
        {
          label: "把握",
          value: triage.confidence == null ? "" : `${Math.round(triage.confidence * 100)}%`,
        },
        { label: "新颖度", value: triage.novelty_zh },
        { label: "受众", value: triage.audience_zh },
      ]}
      label="判定明细"
    />
  );
}

/** Which assets the current judgment called primary, and which it merely mentioned. */
function VerdictAssets({ triage }: { triage: NewsTriageSummary }) {
  const assets = triage.assets ?? [];
  const primary = assets.filter((asset) => asset.role === "primary").map((a) => a.symbol);
  const mentioned = assets.filter((asset) => asset.role !== "primary").map((a) => a.symbol);
  if (!primary.length && !mentioned.length) return null;
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

function TechnicalDetails({ detail }: { detail: NewsEventDetail }) {
  const { event } = detail;
  return (
    <NewsTechnical summary="技术详情（事件 id、话题线、判定与投递记录）">
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
        <KeyValueRow k="judgment_contract_version" v={verdict.judgment_contract_version} />
        <KeyValueRow k="judgment_origin" v={verdict.judgment_origin} />
        <KeyValueRow k="judgment_sha256" v={verdict.judgment_sha256} />
        <KeyValueRow k="model" v={verdict.model ?? "—"} />
        <KeyValueRow k="program_version" v={verdict.program_version} />
        <KeyValueRow k="program_sha256" v={verdict.program_sha256} />
        <KeyValueRow k="rule_baseline_decision" v={verdict.rule_baseline_decision} />
        <KeyValueRow k="final_decision" v={verdict.final_decision} />
        <KeyValueRow k="override_rule" v={verdict.override_rule ?? "—"} />
        <KeyValueRow k="throttled_by" v={verdict.throttled_by ?? "—"} />
        <KeyValueRow k="degraded" v={verdict.degraded ? "true" : "false"} />
        <KeyValueRow k="error_code" v={verdict.error_code ?? "—"} />
        <KeyValueRow k="verdict_direction" v={verdict.verdict.direction} />
        <KeyValueRow k="verdict_magnitude" v={String(verdict.verdict.magnitude)} />
        <KeyValueRow k="verdict_scope" v={verdict.verdict.scope} />
        <KeyValueRow k="verdict_novelty" v={verdict.verdict.novelty} />
        <KeyValueRow k="headline_zh" v={verdict.verdict.headline_zh} />
        <KeyValueRow k="event_family" v={verdict.model_editorial?.taxonomy.event_family ?? "—"} />
        <KeyValueRow k="reader_value" v={verdict.model_editorial?.relevance.reader_value ?? "—"} />
        <KeyValueRow k="evidence_version" v={String(verdict.evidence_version)} />
        <KeyValueRow k="evidence_sha256" v={verdict.evidence_sha256} />
        <KeyValueRow k="focus_fact_id" v={verdict.focus_fact_id} />
        <KeyValueRow k="created_at_ms" v={absoluteTime(verdict.created_at_ms)} />
        <KeyValueRow k="published_at_ms" v={optionalTime(verdict.published_at_ms)} />
      </KeyValue>
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
