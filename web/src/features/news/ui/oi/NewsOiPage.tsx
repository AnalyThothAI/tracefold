import { useTradingGateWithToken } from "@features/trading";
import { newsAlphaPath } from "@shared/routing/paths";
import { Metric, MetricRow } from "@shared/ui/Metric";
import * as PageState from "@shared/ui/PageState";
import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import {
  NEWS_OI_TABS,
  useNewsOiFeedHistoryWithToken,
  useNewsOiFeedWithToken,
  useNewsQuotesWithToken,
  useNewsStatusWithToken,
  type NewsOiTab,
} from "../../api/newsQueries";
import { formatCount } from "../../model/newsLabels";
import { oiTabCount, parseOiTab } from "../../model/oiSignals";
import { NewsPageHeader, NewsPageShell } from "../chrome/NewsChrome";
import { NewsQuoteReadState } from "../chrome/NewsQuoteReadState";

import { NewsOiFrameTable } from "./NewsOiFrameTable";
import { NewsOiGates } from "./NewsOiGates";

import "./newsOi.css";

/**
 * OI 来源与准入审计 — the deterministic open-interest lane, frame by frame (#207, #256, #331).
 *
 * Two questions, and this page asks both about the *Source*: did the telemetry parse and clear the push
 * gates, and did the Signal lane admit it. Whether Alpha then emitted a Signal is a third question with
 * its own frozen evidence, and it is answered on Alpha 判定 — this page links there rather
 * than restating it.
 *
 * Bounded reads: `/api/news/status` for the push gates and 24 h counts, `/api/news/feed` filtered to the
 * deterministic lane for frames, one current-quote batch for the visible assets, and one
 * `/api/trading/gate` batch — which carries the admission configuration as well as the answers, so the
 * panel prints the digest the ledger's rows were actually filed under.
 *
 * **No Intent read (#331).** The trailing column used to load `/api/trading/intents` to show a Case
 * state and an execution state beside each frame, which is three aggregates in one cell and made a failed
 * Intent read render every row as 未成案.
 *
 * What this page deliberately does not do: it draws no open-interest curve (the provider emits a frame
 * only when its own trigger fires, so a line through them would be invented), and it changes no
 * threshold — `news.oi` is operator configuration and this page reports what it currently is.
 */
export function NewsOiPage({ token }: { token: string }) {
  const [searchParams, setSearchParams] = useSearchParams();
  const tab = parseOiTab(searchParams.get("oi"));
  const statusQuery = useNewsStatusWithToken(token);
  const feedQuery = useNewsOiFeedWithToken(token, tab);
  /*
   * The admission ledger for the same window (#269), and the configuration its rows are filed under.
   * One read for both, because they are one aggregate: the panel used to print the operator settings
   * document's 持仓规模 ≥2000 万 while admission actually ran at 500 万.
   */
  const gateQuery = useTradingGateWithToken(token);
  /*
   * Pages behind the first are frozen and only fetched on request; switching tabs drops back to page one,
   * which is what the reader means by switching tabs.
   *
   * The anchor is captured once per tab and then held, the way the Event feed holds its own. Feeding the
   * polled first page's `next_cursor` straight in would move it every time a frame lands, changing the
   * infinite query's key — which discards every page the reader loaded and silently refetches only the
   * first of them.
   */
  const [moreRequested, setMoreRequested] = useState(false);
  const [anchor, setAnchor] = useState<{ cursor: string | null; tab: NewsOiTab } | null>(null);
  const firstPage = feedQuery.data;
  useEffect(() => {
    if (!firstPage || anchor?.tab === tab) return;
    setAnchor({ cursor: firstPage.next_cursor ?? null, tab });
    setMoreRequested(false);
  }, [anchor?.tab, firstPage, tab]);
  const anchorCursor = anchor?.tab === tab ? anchor.cursor : null;
  const historyQuery = useNewsOiFeedHistoryWithToken(token, tab, anchorCursor, moreRequested);
  const pages = historyQuery.data?.pages ?? [];
  const rows = Array.from(
    new Map(
      [firstPage?.events ?? [], ...pages.map((page) => page.events)]
        .flat()
        .map((event) => [event.event_id, event]),
    ).values(),
  );
  const quotesQuery = useNewsQuotesWithToken(
    token,
    rows.flatMap((event) =>
      (event.assets ?? []).filter((asset) => asset.listed).map((asset) => asset.symbol),
    ),
  );
  const quotes = Object.fromEntries(
    (quotesQuery.data?.quotes ?? []).map((quote) => [quote.requested_symbol, quote]),
  );
  const hasMore = Boolean(moreRequested ? historyQuery.hasNextPage : anchorCursor);

  const status = statusQuery.data;
  const pipeline = status?.pipeline;
  const oi = status?.oi;
  const received = pipeline?.telemetry_received_24h;
  const parsed = pipeline?.telemetry_parsed_24h;
  const failed = pipeline?.telemetry_parse_failed_24h;
  const pushed = pipeline?.telemetry_push_24h;
  const byRule = oi?.by_rule_24h;
  const counts = Object.fromEntries(
    NEWS_OI_TABS.map((value) => [value, oiTabCount(value, byRule, pipeline?.telemetry_events_24h)]),
  ) as Record<NewsOiTab, number | null>;
  /*
   * Independent supporting reads sit under this page and each fails on its own, so each is named on its own.
   *
   * A *cold* failure counts, not just a stale refresh: with no admission rules read, the Candidate
   * Gate panel has nothing to print, and four `—` tiles beside 已启用 read as "no admission rule is
   * configured" rather than "we could not ask". The panel says so itself, and this line is what makes
   * it visible above the fold and gives the reader a retry — `PageState.Stale` only offers one when
   * there is a message to attach it to.
   */
  const supportingReadFailures = [gateQuery.isError ? "准入台账" : ""].filter(Boolean);
  return (
    <NewsPageShell archetype="scan" className="news-oi-shell" label="OI 来源与准入审计">
      <NewsPageHeader
        subtitle={
          <>
            遥测帧、解析、推送闸门与资本准入——这一页只答「来源发生了什么」；判定与冻结证据在{" "}
            <Link to={newsAlphaPath()}>Alpha 判定</Link>
          </>
        }
        title="OI 来源与准入审计"
      />

      {/*
       * The cold load in the shape of the page that is coming (#256): the telemetry band, then the frame
       * table. Both regions keep their real geometry, so nothing moves when the two reads answer.
       */}
      {statusQuery.isLoading && !status ? (
        <div className="news-oi-body">
          <PageState.TileSkeleton className="news-oi-metrics" label="正在读取 OI 遥测" />
          <PageState.Loading label="正在读取遥测帧" layout="panel" rows={8} />
        </div>
      ) : null}
      {statusQuery.isError && !status ? (
        <PageState.Error error={statusQuery.error} onRetry={() => void statusQuery.refetch()} />
      ) : null}

      {status ? (
        <PageState.Stale
          failedRefresh={
            supportingReadFailures.length
              ? `${supportingReadFailures.join(" / ")}读取失败，其余内容仍是上次读取。`
              : undefined
          }
          onRetry={() => {
            void quotesQuery.refetch();
            void gateQuery.refetch();
          }}
          updating={statusQuery.isFetching || feedQuery.isFetching || gateQuery.isFetching}
        >
          <div className="news-oi-body">
            <MetricRow className="news-oi-metrics" columns={5} label="过去 24 小时的遥测帧">
              <Metric caption="遥测帧 · 24h" eyebrow="RECEIVED" value={count(received)} />
              <Metric
                caption={
                  received ? `解析成功 ${successPercent(parsed ?? 0, received)}` : "解析成功"
                }
                eyebrow="PARSED"
                value={count(parsed)}
              />
              <Metric
                caption="过全部闸门"
                eyebrow="ELIGIBLE"
                value={count(byRule?.opening_move_with_whale_concentration ?? 0)}
              />
              <Metric caption="已推送" eyebrow="PUSHED" tone="accent" value={count(pushed)} />
              <Metric
                caption="模板变了才会涨"
                eyebrow="FAILED"
                tone={failed ? "caution" : "plain"}
                value={count(failed)}
              />
            </MetricRow>

            <div className="news-oi-columns">
              <NewsOiGates
                byRule={byRule ?? {}}
                gate={gateQuery.data?.config}
                /*
                 * Whether the admission rules were read at all. Four `—` tiles read as "no admission
                 * rule is configured", which is the failure mode this page must never present: an
                 * unread threshold and an absent one are different facts.
                 */
                gateUnread={!gateQuery.data}
                policy={oi?.policy ?? null}
              />
            </div>

            {/*
             * The failed request replaces the rows, never the tabs: a reader whose 未达阈值 page 5xx'd has to
             * be able to click back to 全部 without editing the URL.
             */}
            <NewsQuoteReadState query={quotesQuery}>
              <NewsOiFrameTable
                counts={counts}
                error={feedQuery.isError && !feedQuery.data ? feedQuery.error : null}
                gate={gateQuery.data}
                gateError={gateQuery.isError && !gateQuery.data}
                hasMore={hasMore}
                loadingMore={
                  historyQuery.isFetchingNextPage || (moreRequested && historyQuery.isLoading)
                }
                onLoadMore={() => {
                  if (!moreRequested) setMoreRequested(true);
                  else void historyQuery.fetchNextPage();
                }}
                onRetry={() => void feedQuery.refetch()}
                onTabChange={(next) => setSearchParams(nextOiParams(next), { replace: true })}
                rows={rows}
                quotes={quotes}
                tab={tab}
              />
            </NewsQuoteReadState>
          </div>
        </PageState.Stale>
      ) : null}
    </NewsPageShell>
  );
}

function count(value: number | undefined): string {
  return value == null ? "—" : formatCount(value);
}

function successPercent(value: number, total: number): string {
  return total > 0 ? `${((value / total) * 100).toFixed(1)}%` : "—";
}

function nextOiParams(tab: NewsOiTab): URLSearchParams {
  const params = new URLSearchParams();
  if (tab !== "all") params.set("oi", tab);
  return params;
}
