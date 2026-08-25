import { newsEventPath } from "@shared/routing/paths";
import { ActionButton } from "@shared/ui/ActionButton";
import { Card } from "@shared/ui/Card";
import * as PageState from "@shared/ui/PageState";
import { ChevronRight } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router-dom";

import type { NewsFeedEvent, NewsOiTab, NewsOiTradeFloors } from "../../api/newsQueries";
import { NEWS_OI_TABS } from "../../api/newsQueries";
import { clockTime, displayTime, formatCount } from "../../model/newsLabels";
import { formatBps, priceTone, reactionPlaceholder, reactionValue } from "../../model/newsPrice";
import {
  OI_TAB_LABELS,
  oiBuckets,
  oiChangeLabel,
  oiPercent,
  oiRankLabel,
  oiRuleLabel,
  oiValueZh,
} from "../../model/oiSignals";
import { NewsEmptyNote } from "../chrome/NewsChrome";
import { NewsDirectionChip } from "../chrome/NewsDirectionChip";
import { NewsOutcomeBadge } from "../chrome/NewsOutcomeBadge";

import { NewsOiSource } from "./NewsOiSource";

import "./newsOiFrameTable.css";

/**
 * The frames themselves: one row per telemetry Event, expandable to the provider's line and the judge's own
 * trace.
 *
 * Every measurement here comes from the `oi` block the server folded out of `oi_judgment_trace()`. A row
 * whose block is missing — an Event judged before #207 shipped, or one still awaiting a verdict — says so
 * and renders its other columns; it never re-parses `leader_title` to fill the gap.
 */
export function NewsOiFrameTable({
  counts,
  error,
  floors,
  hasMore,
  loadingMore,
  onLoadMore,
  onRetry,
  onTabChange,
  rows,
  tab,
}: {
  counts: Record<NewsOiTab, number | null>;
  error: unknown;
  floors: NewsOiTradeFloors;
  hasMore: boolean;
  loadingMore: boolean;
  onLoadMore: () => void;
  onRetry: () => void;
  onTabChange: (tab: NewsOiTab) => void;
  rows: readonly NewsFeedEvent[];
  tab: NewsOiTab;
}) {
  return (
    <Card
      flush
      hint="红是利多、绿是利空，是阅读卡沿用的词——持仓涨不等于价格涨，右侧 1H / 4H 才是价格"
      title="遥测帧"
    >
      <div aria-label="按判定筛选" className="news-oi-tabs" role="tablist">
        {NEWS_OI_TABS.map((value) => (
          <button
            aria-selected={tab === value}
            className="news-oi-tab"
            data-active={tab === value || undefined}
            key={value}
            onClick={() => onTabChange(value)}
            role="tab"
            type="button"
          >
            {OI_TAB_LABELS[value]}
            {/*
             * The server's 24 h aggregate, not a count of the rows below: the tab filters the whole window
             * server-side while the table shows one page of it, and deriving the number from the page would
             * make an empty page read as an empty window.
             *
             * The two are anchored differently — `by_rule_24h` counts verdicts by `created_at_ms`, the feed
             * bounds Events by `opened_at_ms` — so at the window edge they can disagree by a frame or two,
             * the same asymmetry the 24 h funnel card carries. The source line below says so.
             */}
            {counts[value] == null ? null : (
              <span aria-hidden className="news-oi-tab-count">
                {formatCount(counts[value] as number)}
              </span>
            )}
          </button>
        ))}
      </div>

      {error ? <PageState.Error error={error} onRetry={onRetry} /> : null}

      {!error && rows.length === 0 ? (
        <NewsEmptyNote>这个窗口里没有符合当前判定的遥测帧。</NewsEmptyNote>
      ) : null}

      {!error && rows.length > 0 ? (
        <div className="news-oi-table">
          <div aria-hidden className="news-oi-head">
            <span>TIME</span>
            <span>SYMBOL</span>
            <span className="news-oi-num">OI 变动</span>
            <span className="news-oi-num">持仓</span>
            <span className="news-oi-num">鲸鱼占比</span>
            <span className="news-oi-num">鲸鱼盈利</span>
            <span className="news-oi-num">窗口</span>
            <span>研究分桶</span>
            <span>判定</span>
            <span className="news-oi-num">1H / 4H</span>
          </div>
          {rows.map((event) => (
            <FrameRow event={event} floors={floors} key={event.event_id} />
          ))}
        </div>
      ) : null}

      {/*
       * An explicit action, never automatic scroll (`docs/FRONTEND.md`). A 24 h window holds more frames
       * than one page, so without this the table would sit at 50 rows under a tab that names 136.
       */}
      {!error && hasMore ? (
        <div className="news-oi-more">
          <ActionButton disabled={loadingMore} onClick={onLoadMore}>
            {loadingMore ? "正在加载" : "加载更多帧"}
          </ActionButton>
          <small>已加载 {formatCount(rows.length)} 条；页签计数是过去 24 小时的全量</small>
        </div>
      ) : null}

      <NewsOiSource
        note="四个测量值、窗口名次与闸门名都来自服务端已落库的判定痕迹；1H/4H 是事件锚定的定格测量，不是现价。页签计数按判定时间算、行按事件时间算，窗口边缘上二者可差一两条"
        path="GET /api/news/feed?admission=telemetry_deterministic&hours=24"
      />
    </Card>
  );
}

function FrameRow({ event, floors }: { event: NewsFeedEvent; floors: NewsOiTradeFloors }) {
  const [open, setOpen] = useState(false);
  const oi = event.oi ?? null;
  const triage = event.triage ?? null;
  const symbol = triage?.assets?.find((asset) => asset.role === "primary")?.symbol ?? "";
  const buckets = oiBuckets(oi, floors);
  const withheld = event.outcome.group !== "pushed";
  return (
    <article
      className="news-oi-row"
      data-open={open || undefined}
      data-withheld={withheld || undefined}
    >
      <button
        aria-expanded={open}
        className="news-oi-row-main"
        onClick={() => setOpen((value) => !value)}
        type="button"
      >
        <span className="news-oi-time" title={displayTime(event.opened_at_ms)}>
          {clockTime(event.opened_at_ms)}
        </span>
        <span className="news-oi-symbol">
          <b>{symbol || "—"}</b>
          {triage ? <NewsDirectionChip triage={triage} withStrength={false} /> : null}
        </span>
        <span className="news-oi-num news-oi-change">{oiChangeLabel(oi)}</span>
        <span className="news-oi-num">{oiValueZh(oi?.oi_value_usd)}</span>
        <span
          className="news-oi-num news-oi-ratio"
          data-gate={oi?.rule === "whale_ratio_below_threshold" || undefined}
        >
          {oiPercent(oi?.whale_oi_ratio_bps)}
        </span>
        <span className="news-oi-num">{oiPercent(oi?.whale_long_profit_bps)}</span>
        <span className="news-oi-num" data-gate={oi?.rule === "beyond_window_rank" || undefined}>
          {oiRankLabel(oi)}
        </span>
        <span className="news-oi-buckets">
          {buckets.length === 0
            ? null
            : buckets.map((bucket) => (
                <span
                  className="news-oi-bucket"
                  data-tone={bucket.tone}
                  key={bucket.label}
                  title={bucket.title}
                >
                  {bucket.label}
                </span>
              ))}
        </span>
        <span className="news-oi-verdict">
          <NewsOutcomeBadge outcome={event.outcome} />
          {oi?.rule ? <code title={oiRuleLabel(oi.rule)}>{oi.rule}</code> : null}
        </span>
        <span className="news-oi-num news-oi-reaction">
          <ReactionValue event={event} horizon="1h" />
          <span aria-hidden className="news-oi-reaction-slash">
            /
          </span>
          <ReactionValue event={event} horizon="4h" />
        </span>
      </button>
      {open ? <FrameDetail event={event} oi={oi} /> : null}
    </article>
  );
}

function ReactionValue({ event, horizon }: { event: NewsFeedEvent; horizon: "1h" | "4h" }) {
  const value = reactionValue(event.reaction, horizon);
  // A horizon that has not matured is `未到期`, never `0.00%`: a missing measurement and a flat market are
  // different facts and only one of them is a number.
  if (value == null) return <em>{reactionPlaceholder(event.reaction, horizon)}</em>;
  return <b data-dir={priceTone(value)}>{formatBps(value)}</b>;
}

function FrameDetail({ event, oi }: { event: NewsFeedEvent; oi: NewsFeedEvent["oi"] }) {
  return (
    <div className="news-oi-detail">
      <div className="news-oi-detail-left">
        <small className="news-oi-detail-label">供应商原帧</small>
        <code className="news-oi-raw">{event.leader_title}</code>
        <small className="news-oi-detail-label">阅读卡标题</small>
        <p className="news-oi-headline">{event.triage?.headline_zh || "—"}</p>
        <Link className="news-oi-open" to={newsEventPath(event.event_id)}>
          打开事件详情
          <ChevronRight aria-hidden />
        </Link>
      </div>
      <div className="news-oi-detail-right">
        <small className="news-oi-detail-label">判定痕迹</small>
        {oi ? (
          <dl className="news-oi-trace">
            {traceEntries(oi).map(([key, value]) => (
              <div key={key}>
                <dt>{key}</dt>
                <dd>{value}</dd>
              </div>
            ))}
          </dl>
        ) : (
          <p className="news-oi-trace-missing">
            这条事件没有 <code>oi</code> 段——它的判定早于本页上线，或还没落判定。原帧仍在左侧，完整
            <code>trace</code> 在事件详情的技术详情里。
          </p>
        )}
        <p className="news-oi-trace-note">
          键名即后端 <code>oi_judgment_trace()</code> 写下的键，本页不另起词表。
        </p>
      </div>
    </div>
  );
}

/** The trace as the judge wrote it: the keys it used, in the order the rule reads them. */
function traceEntries(oi: NonNullable<NewsFeedEvent["oi"]>): Array<[string, string]> {
  const entries: Array<[string, string]> = [
    ["parsed", String(oi.parsed)],
    ["rule", oi.rule || "—"],
  ];
  if (oi.parsed) {
    entries.push(
      ["oi_change_bps", numeric(oi.oi_change_bps)],
      ["oi_value_usd", numeric(oi.oi_value_usd)],
      ["whale_long_profit_bps", numeric(oi.whale_long_profit_bps)],
      ["whale_oi_ratio_bps", numeric(oi.whale_oi_ratio_bps)],
      ["eligible_rank_in_window", numeric(oi.eligible_rank_in_window)],
      ["rank_semantics", oi.rank_semantics ?? "—"],
      ["policy.whale_oi_ratio_above_bps", numeric(oi.whale_oi_ratio_above_bps)],
      ["policy.max_rank_in_window", numeric(oi.max_rank_in_window)],
      ["policy.oi_change_at_least_bps", numeric(oi.oi_change_at_least_bps)],
      ["policy.window_ms", numeric(oi.window_ms)],
    );
    return entries;
  }
  entries.push(
    ["failure_stage", oi.failure_stage ?? "—"],
    ["parser_version", oi.parser_version ?? "—"],
    ["title_sha256", oi.title_sha256 ?? "—"],
  );
  return entries;
}

function numeric(value: number | null | undefined): string {
  return value == null ? "—" : String(value);
}
