import {
  useTradingGateWithToken,
  useTradingOrdersWithToken,
  useTradingStatusWithToken,
} from "@features/trading";
import { newsLeveragePath } from "@shared/routing/paths";
import { Metric, MetricRow } from "@shared/ui/Metric";
import * as PageState from "@shared/ui/PageState";
import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import {
  NEWS_OI_TABS,
  useNewsOiFeedHistoryWithToken,
  useNewsOiFeedWithToken,
  useNewsStatusWithToken,
  type NewsOiTab,
} from "../../api/newsQueries";
import { formatCount } from "../../model/newsLabels";
import { oiTabCount, parseOiTab } from "../../model/oiSignals";
import { NewsPageHeader, NewsPageShell } from "../chrome/NewsChrome";

import { NewsOiFrameTable } from "./NewsOiFrameTable";
import { NewsOiGates } from "./NewsOiGates";

import "./newsOi.css";

/**
 * OI 遥测审计 — #137's deterministic open-interest lane, audited frame by frame (#207, #256).
 *
 * The v7 artifact moved this page out of the workbench and into 数据健康, and the rename is the point: the
 * question here is whether the telemetry itself parsed, cleared the push gates and occupied a window slot.
 * "Is there a trade in this" is a different question with different thresholds, and it is not asked here.
 *
 * Three bounded reads: `/api/news/status` for thresholds and 24 h counts, `/api/news/feed` filtered to the
 * deterministic lane for frames, and one `/api/trading/orders` batch for exact Event-to-ledger joins.
 *
 * What this page deliberately does not do: it draws no open-interest curve (the provider emits a frame only
 * when its own trigger fires, so there are no samples between them and a line through them would be
 * invented), it puts no current quote on a frame (a price that changes every few seconds beside a fixed
 * post-event measurement reads as a market screen), and it does not let anyone change a threshold here —
 * `news.oi` is operator configuration and this page only reports what it currently is.
 */
export function NewsOiPage({ token }: { token: string }) {
  const [searchParams, setSearchParams] = useSearchParams();
  const tab = parseOiTab(searchParams.get("oi"));
  const statusQuery = useNewsStatusWithToken(token);
  const feedQuery = useNewsOiFeedWithToken(token, tab);
  const tradingQuery = useTradingOrdersWithToken(token);
  /*
   * The admission ledger for the same window (#269). Its own read rather than a field on the orders
   * batch: it is a different population — every source the lane saw, not the ones that became a case —
   * and it is the only thing that can tell a reader *why* a frame has no case.
   */
  const gateQuery = useTradingGateWithToken(token);
  /*
   * And the rules that ledger's rows are filed under. `oi.trade_floors` is News republishing the
   * operator's `trading` settings document; since #264 admission has one owner with its own digest, and
   * the panel was printing 持仓规模 ≥2000 万 while the gate admitted at 500 万.
   */
  const tradingStatusQuery = useTradingStatusWithToken(token);
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
  return (
    <NewsPageShell archetype="scan" className="news-oi-shell" label="OI 遥测审计">
      <NewsPageHeader
        subtitle={
          <>
            遥测帧、解析、闸门与推送窗口占用——推送答「读者看什么」；交易判断在{" "}
            <Link to={newsLeveragePath()}>杠杆异动</Link>
          </>
        }
        title="OI 遥测审计"
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
            (tradingQuery.isError && tradingQuery.data) || (gateQuery.isError && gateQuery.data)
              ? "交易账本刷新失败，继续显示上次读取。"
              : undefined
          }
          onRetry={() => {
            void tradingQuery.refetch();
            void gateQuery.refetch();
          }}
          updating={
            statusQuery.isFetching ||
            feedQuery.isFetching ||
            tradingQuery.isFetching ||
            gateQuery.isFetching
          }
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
                floors={oi?.trade_floors ?? EMPTY_FLOORS}
                gate={tradingStatusQuery.data?.gate}
                policy={oi?.policy ?? null}
                strategies={tradingStatusQuery.data?.strategies ?? []}
              />
            </div>

            {/*
             * The failed request replaces the rows, never the tabs: a reader whose 未达阈值 page 5xx'd has to
             * be able to click back to 全部 without editing the URL.
             */}
            <NewsOiFrameTable
              counts={counts}
              error={feedQuery.isError && !feedQuery.data ? feedQuery.error : null}
              floors={oi?.trade_floors ?? EMPTY_FLOORS}
              gate={gateQuery.data}
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
              tab={tab}
              trading={tradingQuery.data}
              tradingError={
                (tradingQuery.isError && !tradingQuery.data) ||
                (gateQuery.isError && !gateQuery.data)
              }
            />
          </div>
        </PageState.Stale>
      ) : null}
    </NewsPageShell>
  );
}

/**
 * A console older than the API it is talking to still has to render. Zeroes here are honest: they are the
 * defaults the schema itself declares, and every threshold they feed is shown beside its own source line.
 */
const EMPTY_FLOORS = {
  allow_short: false,
  enabled: false,
  max_price_move_bps: 0,
  min_oi_value_usd: 0,
  min_price_move_bps: 0,
  min_whale_long_profit_bps: 0,
  mode: "paper",
  pre_move_lookback_ms: 0,
};

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
