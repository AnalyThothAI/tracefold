import { PageShell } from "@shared/ui/PageShell";
import * as PageState from "@shared/ui/PageState";
import { SourceLine } from "@shared/ui/SourceLine";
import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";

import {
  useNewsMarketHistoryWithToken,
  useNewsMarketWithToken,
  useNewsStatusWithToken,
} from "../../api/newsQueries";
import { mergeMarketGroups, nextMarketParams, parseMarketKinds } from "../../model/marketFacts";
import { optionalTime } from "../../model/newsLabels";
import { NewsPageHeader } from "../chrome/NewsChrome";

import { NewsMarketGroupTable } from "./NewsMarketGroupTable";
import { NewsMarketSources } from "./NewsMarketSources";

import "./newsMarket.css";

/**
 * 市场事实 — what OpenNews reported about the market, as it was stored (#553 PR-1).
 *
 * OI frames, liquidations, smart-money prints and sources we have no parser for are no longer Events. They
 * are market observations: rows in their own table, read here through `/api/news/market`, collapsed to one
 * group per run of consecutive observations of the same subject. Nothing on this page is judged, scored or
 * admitted — the Event feed's vocabulary does not apply, and neither does the Signal lane's.
 *
 * **Three independent reads, three independent failures.** The list is the page: `/api/news/market`
 * answering is the only precondition for showing a row. `/api/news/status` is a separate read behind one
 * strip about the wire, and it failing may not blank the observations — the page it replaced wrapped its
 * whole body in a `PageState.Stale` gated on the status query, so a 5xx on a pipeline dashboard endpoint
 * took the market data with it. And this page reads no Trading endpoint at all: a market observation is not
 * an Event, so there is no `event_id` for an admission verdict to join on, and there is nothing here for
 * `/api/trading/gate` to answer.
 *
 * **Push is reported, never assumed.** There is no page-level "is push on" banner: it would be a second,
 * weaker answer to a question every row already answers.
 * Every group carries its own `notification_status` and `notification_reason`, kept apart from
 * `parse_status` and `parse_error` because a record that parsed cleanly and was not pushed, and one that
 * never parsed, are two different states an operator acts on differently.
 */
export function NewsMarketPage({ token }: { token: string }) {
  const [searchParams, setSearchParams] = useSearchParams();
  const kinds = parseMarketKinds(searchParams.get("kind"));
  const kindKey = kinds.join(",");
  const marketQuery = useNewsMarketWithToken(token, kinds);
  /*
   * The wire behind the facts, and the one question the stored rows cannot answer: an empty window is a
   * quiet market or a dropped connection, and only `/api/news/status` knows which. It is a supporting read
   * and is drawn as one — its failure is a note inside this strip and reaches nothing else on the page.
   */
  const statusQuery = useNewsStatusWithToken(token);

  /*
   * Pages behind the first are frozen and only fetched on request; changing the filter drops back to page
   * one, which is what the reader means by changing the filter. The anchor is captured once per filter and
   * then held — feeding the polled first page's `next_cursor` straight in would move the infinite query's
   * key every time an observation lands.
   */
  const [moreRequested, setMoreRequested] = useState(false);
  const [anchor, setAnchor] = useState<{ cursor: string | null; kindKey: string } | null>(null);
  const firstPage = marketQuery.data;
  useEffect(() => {
    if (!firstPage || anchor?.kindKey === kindKey) return;
    setAnchor({ cursor: firstPage.next_cursor ?? null, kindKey });
    setMoreRequested(false);
  }, [anchor?.kindKey, firstPage, kindKey]);
  const anchorCursor = anchor?.kindKey === kindKey ? anchor.cursor : null;
  const historyQuery = useNewsMarketHistoryWithToken(token, kinds, anchorCursor, moreRequested);
  const pages = historyQuery.data?.pages ?? [];
  /*
   * Freshest page first, so a run that gained an observation between "load more" and the next poll
   * keeps the poll's copy instead of rendering beside the frozen one.
   */
  const groups = mergeMarketGroups([firstPage?.groups ?? [], ...pages.map((page) => page.groups)]);
  const hasMore = Boolean(moreRequested ? historyQuery.hasNextPage : anchorCursor);

  return (
    <PageShell archetype="scan" className="news-market-shell" label="市场事实">
      <NewsPageHeader
        subtitle="OpenNews 的市场观测按事实入库：持仓异动、强平、聪明钱，以及没有解析器的原文来源。这一页只答「收到了什么、解析成什么、推没推」。"
        title="市场事实"
      />

      {marketQuery.isLoading && !firstPage ? (
        <div className="news-market-body">
          <PageState.TileSkeleton
            className="news-market-sources"
            label="正在读取来源汇总"
            tiles={4}
          />
          <PageState.Loading label="正在读取市场观测" layout="panel" rows={8} />
        </div>
      ) : null}
      {marketQuery.isError && !firstPage ? (
        <PageState.Error error={marketQuery.error} onRetry={() => void marketQuery.refetch()} />
      ) : null}

      {firstPage ? (
        <PageState.Stale
          failedRefresh={
            marketQuery.isError ? "市场观测刷新失败，下面仍是上次读取的结果。" : undefined
          }
          onRetry={() => void marketQuery.refetch()}
          updating={marketQuery.isFetching}
        >
          <div className="news-market-body">
            <div className="news-market-notes">
              <IngestNote query={statusQuery} />
            </div>

            <NewsMarketSources selected={kinds} sources={firstPage.sources} />

            <NewsMarketGroupTable
              groups={groups}
              hasMore={hasMore}
              kinds={kinds}
              loadingMore={
                historyQuery.isFetchingNextPage || (moreRequested && historyQuery.isLoading)
              }
              onKindsChange={(next) => setSearchParams(nextMarketParams(next), { replace: true })}
              onLoadMore={() => {
                if (!moreRequested) setMoreRequested(true);
                else void historyQuery.fetchNextPage();
              }}
              filters={firstPage.filters}
              scanTruncated={firstPage.scan_truncated}
              token={token}
            />

            <SourceLine
              note="推送状态、解析状态与来源计数都来自这一次读取，没有第二个端点参与"
              path="GET /api/news/market → groups[] · sources[]"
            />
          </div>
        </PageState.Stale>
      ) : null}
    </PageShell>
  );
}

/**
 * The ingest wire, from the pipeline status read.
 *
 * Its own failure and nothing else's: with no status answer this note says the read failed and the
 * observations beside it are untouched.
 */
function IngestNote({ query }: { query: ReturnType<typeof useNewsStatusWithToken> }) {
  if (query.isError && !query.data) {
    return (
      <p className="news-market-note" data-tone="caution">
        <small>入站连接</small>
        <b>状态未知</b>
        <em>读取 /api/news/status 失败；下面的观测来自 /api/news/market，未受影响。</em>
      </p>
    );
  }
  const ingest = query.data?.ingest;
  if (!ingest) {
    return (
      <p className="news-market-note" data-tone="plain">
        <small>入站连接</small>
        <b>读取中</b>
        <em>正在读取 /api/news/status。</em>
      </p>
    );
  }
  return (
    <p className="news-market-note" data-tone={ingest.connected ? "done" : "caution"}>
      <small>入站连接</small>
      <b>{ingest.connected ? "已连接" : "已断开"}</b>
      <em>
        最近一帧 {optionalTime(ingest.last_frame_at_ms)}
        {ingest.last_error_code ? ` · ${ingest.last_error_code}` : ""}
      </em>
    </p>
  );
}
