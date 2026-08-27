import {
  tradingGateByEventId,
  tradingOiCellCopy,
  tradingOiLedgerByEventId,
  tradingOiTraceEntries,
  type TradingGate,
  type TradingGateDecision,
  type TradingOiLedgerEntry,
  type TradingOiLookup,
  type TradingOrders,
} from "@features/trading";
import { newsEventPath, newsSymbolPath } from "@shared/routing/paths";
import { useRouteReferrer } from "@shared/routing/routeReferrer";
import { ActionButton } from "@shared/ui/ActionButton";
import * as PageState from "@shared/ui/PageState";
import { ChevronRight } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router-dom";

import type { NewsFeedEvent, NewsOiTab, NewsOiTradeFloors } from "../../api/newsQueries";
import { NEWS_OI_TABS } from "../../api/newsQueries";
import { clockTime, displayTime, formatCount } from "../../model/newsLabels";
import {
  formatBps,
  formatPrice,
  priceTone,
  reactionPlaceholder,
  reactionValue,
} from "../../model/newsPrice";
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

import "./newsOiFrameTable.css";

/** One deterministic telemetry Event per row, with an exact optional capital-ledger join. */
export function NewsOiFrameTable({
  counts,
  error,
  floors,
  gate,
  hasMore,
  loadingMore,
  onLoadMore,
  onRetry,
  onTabChange,
  rows,
  tab,
  trading,
  tradingError,
}: {
  counts: Record<NewsOiTab, number | null>;
  error: unknown;
  floors: NewsOiTradeFloors;
  gate: TradingGate | undefined;
  hasMore: boolean;
  loadingMore: boolean;
  onLoadMore: () => void;
  onRetry: () => void;
  onTabChange: (tab: NewsOiTab) => void;
  rows: readonly NewsFeedEvent[];
  tab: NewsOiTab;
  trading: TradingOrders | undefined;
  tradingError: boolean;
}) {
  const ledger = tradingOiLedgerByEventId(trading);
  const gateByEvent = tradingGateByEventId(gate);
  return (
    <section className="news-oi-frame-panel" aria-label="遥测帧">
      <div className="news-oi-frame-toolbar">
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
              {counts[value] == null ? null : (
                <span aria-hidden className="news-oi-tab-count">
                  {formatCount(counts[value] as number)}
                </span>
              )}
            </button>
          ))}
        </div>
        <small>红是利多、绿是利空；价格看 1H/4H 与交易列</small>
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
            <span className="news-oi-num">价格</span>
            <span className="news-oi-num">OI 变动</span>
            <span className="news-oi-num">持仓</span>
            <span className="news-oi-num">鲸鱼占比</span>
            <span className="news-oi-num">鲸鱼盈利</span>
            <span className="news-oi-num">窗口</span>
            <span>研究分桶</span>
            <span>判定</span>
            <span className="news-oi-num">1H / 4H</span>
            <span>交易 · OI_ONLY</span>
          </div>
          {rows.map((event) => (
            <FrameRow
              event={event}
              floors={floors}
              gateAnswered={Boolean(gate)}
              gateComplete={gate?.complete ?? false}
              gateDecision={gateByEvent.get(event.event_id)}
              key={event.event_id}
              ledgerEntry={ledger.get(event.event_id)}
              ledgerComplete={trading?.complete ?? false}
              tradingError={tradingError}
              tradingLoaded={Boolean(trading)}
            />
          ))}
        </div>
      ) : null}

      {!error && hasMore ? (
        <div className="news-oi-more">
          <ActionButton disabled={loadingMore} onClick={onLoadMore}>
            {loadingMore ? "正在加载" : "加载更多帧"}
          </ActionButton>
          <small>已加载 {formatCount(rows.length)} 条；页签计数是过去 24 小时的全量</small>
        </div>
      ) : null}
    </section>
  );
}

function FrameRow({
  event,
  floors,
  gateAnswered,
  gateComplete,
  gateDecision,
  ledgerComplete,
  ledgerEntry,
  tradingError,
  tradingLoaded,
}: {
  event: NewsFeedEvent;
  floors: NewsOiTradeFloors;
  gateAnswered: boolean;
  gateComplete: boolean;
  gateDecision: TradingGateDecision | undefined;
  ledgerComplete: boolean;
  ledgerEntry: TradingOiLedgerEntry | undefined;
  tradingError: boolean;
  tradingLoaded: boolean;
}) {
  const [open, setOpen] = useState(false);
  const oi = event.oi ?? null;
  const triage = event.triage ?? null;
  const symbol = oi?.symbol ?? "";
  const buckets = oiBuckets(oi, floors);
  const withheld = event.outcome.group !== "pushed";
  const tradingLookup: TradingOiLookup = {
    complete: ledgerComplete,
    entry: ledgerEntry,
    eventId: event.event_id,
    gate: gateDecision,
    gateAnswered,
    gateComplete,
    loadFailed: tradingError,
    loaded: tradingLoaded,
  };
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
        <span className="news-oi-num news-oi-price" title="帧时标记价（p0），不是现价">
          {formatPrice(event.reaction?.p0)}
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
          {buckets.map((bucket) => (
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
        {/* One cell, both horizons. They are one reading of the same frame at two distances, and a column
            each let the row spend 132px saying so. */}
        <span className="news-oi-num news-oi-reaction">
          <ReactionValue event={event} horizon="1h" />
          {/* Not `aria-hidden`. The row is one button and its accessible name is its whole text, so
              hiding the only delimiter ran the two horizons together as `+15.96%未到期`. */}
          <span className="news-oi-reaction-slash">/</span>
          <ReactionValue event={event} horizon="4h" />
        </span>
        <TradingCell lookup={tradingLookup} />
      </button>
      {open ? <FrameDetail event={event} tradingLookup={tradingLookup} /> : null}
    </article>
  );
}

function ReactionValue({ event, horizon }: { event: NewsFeedEvent; horizon: "1h" | "4h" }) {
  const value = reactionValue(event.reaction, horizon);
  if (value == null) return <em>{reactionPlaceholder(event.reaction, horizon)}</em>;
  return <b data-dir={priceTone(value)}>{formatBps(value)}</b>;
}

function TradingCell({ lookup }: { lookup: TradingOiLookup }) {
  const copy = tradingOiCellCopy(lookup);
  return (
    <span className="news-oi-trading-cell" title={copy.title}>
      {/* The lane's own quadrant, from `regime` on the case — not a pre-frame price this page does not
          have. It leads the cell because it is the first gate the decision beside it went through, and it
          is a separate field from `secondary` so a gate stage can never arrive dressed as a quadrant. */}
      {copy.quadrant ? <span className="news-oi-quadrant">{copy.quadrant}</span> : null}
      {copy.secondary ? <small>{copy.secondary}</small> : null}
      <b>{copy.primary}</b>
    </span>
  );
}

function FrameDetail({
  event,
  tradingLookup,
}: {
  event: NewsFeedEvent;
  tradingLookup: TradingOiLookup;
}) {
  const referrer = useRouteReferrer();
  const symbol = event.oi?.symbol ?? "";
  return (
    <div className="news-oi-detail">
      <div className="news-oi-detail-left">
        <small className="news-oi-detail-label">供应商原帧</small>
        <code className="news-oi-raw">{event.leader_title}</code>
        <small className="news-oi-detail-label">阅读卡标题</small>
        <p className="news-oi-headline">{event.triage?.headline_zh || "—"}</p>
        <span className="news-oi-detail-links">
          <Link className="news-oi-open" to={newsEventPath(event.event_id)}>
            打开事件详情 <ChevronRight aria-hidden />
          </Link>
          {symbol ? (
            <Link className="news-oi-open" state={referrer} to={newsSymbolPath(symbol)}>
              代币页 {symbol} <ChevronRight aria-hidden />
            </Link>
          ) : null}
        </span>
      </div>
      <TracePanel
        label="判定痕迹 · OI_JUDGMENT_TRACE"
        entries={event.oi ? traceEntries(event.oi) : null}
      />
      <TradingTrace lookup={tradingLookup} />
    </div>
  );
}

function TracePanel({
  label,
  entries,
}: {
  label: string;
  entries: Array<[string, string]> | null;
}) {
  return (
    <div className="news-oi-detail-right">
      <small className="news-oi-detail-label">{label}</small>
      {entries ? (
        <dl className="news-oi-trace">
          {entries.map(([key, value]) => (
            <div key={key}>
              <dt>{key}</dt>
              <dd>{value}</dd>
            </div>
          ))}
        </dl>
      ) : (
        <p className="news-oi-trace-missing">这条事件没有可展示的已落库痕迹。</p>
      )}
    </div>
  );
}

function TradingTrace({ lookup }: { lookup: TradingOiLookup }) {
  return <TracePanel label="交易判定 · ?LANE=OI" entries={tradingOiTraceEntries(lookup)} />;
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
