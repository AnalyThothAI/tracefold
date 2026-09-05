import { ActionButton } from "@shared/ui/ActionButton";
import * as PageState from "@shared/ui/PageState";
import { useState } from "react";

import {
  NEWS_MARKET_KINDS,
  useNewsMarketItemWithToken,
  type NewsMarket,
  type NewsMarketGroup,
  type NewsMarketItem,
  type NewsMarketKind,
  type NewsMarketObservation,
} from "../../api/newsQueries";
import {
  marketKindLabel,
  marketKindTitle,
  marketObservationTrace,
  marketParseLabel,
  marketSubject,
  toggleMarketKind,
} from "../../model/marketFacts";
import { clockTime, displayTime, formatCount } from "../../model/newsLabels";
import { NewsEmptyNote } from "../chrome/NewsChrome";

import "./newsMarketGroupTable.css";

/**
 * One row per group: a run of consecutive observations of the same subject, collapsed to its newest member.
 *
 * The row carries two answers of equal standing and keeps them apart. `parse_status` / `parse_error` say
 * what the parser could read out of the provider's record. `notification_status` / `notification_reason`
 * say what the notification owner did with it. They are not two views of one verdict — a record can parse
 * cleanly and never be pushed, and one that never parsed is still retained with its raw line — so they
 * never share a column, a colour, or a word.
 */
export function NewsMarketGroupTable({
  filters,
  groups,
  hasMore,
  kinds,
  loadingMore,
  onKindsChange,
  onLoadMore,
  scanTruncated,
  token,
}: {
  filters: NewsMarket["filters"];
  groups: readonly NewsMarketGroup[];
  hasMore: boolean;
  kinds: readonly NewsMarketKind[];
  loadingMore: boolean;
  onKindsChange: (kinds: NewsMarketKind[]) => void;
  onLoadMore: () => void;
  scanTruncated: boolean;
  token: string;
}) {
  return (
    <section aria-label="市场观测" className="news-market-panel">
      <div className="news-market-toolbar">
        <div aria-label="按市场类型筛选" className="news-market-kinds" role="group">
          {/*
           * One meaning per state, the way the feed's own channel chips work: a lit chip is a kind the
           * reader explicitly selected, and none lit is the absence of a filter rather than an empty
           * window. Lighting all four when nothing is selected would make the pressed state say two
           * different things — "I picked this" and "this is visible" — on the same control.
           */}
          {NEWS_MARKET_KINDS.map((kind) => (
            <button
              aria-pressed={kinds.includes(kind)}
              className="news-market-kind-filter"
              data-active={kinds.includes(kind) || undefined}
              key={kind}
              onClick={() => onKindsChange(toggleMarketKind(kinds, kind))}
              title={marketKindTitle(kind)}
              type="button"
            >
              {marketKindLabel(kind)}
            </button>
          ))}
        </div>
        <small title={`${displayTime(filters.from_ms)} → ${displayTime(filters.to_ms)}`}>
          {kinds.length ? `${kinds.length} / ${NEWS_MARKET_KINDS.length} 类` : "全部类型"} · 窗口{" "}
          {displayTime(filters.from_ms)} → {displayTime(filters.to_ms)}
        </small>
      </div>

      {groups.length === 0 ? (
        <NewsEmptyNote>这个窗口里没有符合当前筛选的市场观测。</NewsEmptyNote>
      ) : (
        <div className="news-market-rows">
          {groups.map((group) => (
            <GroupRow
              group={group}
              key={`${group.group_key}:${group.latest.item_id}`}
              token={token}
            />
          ))}
        </div>
      )}

      {/*
       * The run counts above are what one bounded page could see. When a page fills that bound the
       * server says so, and a floor is reported as a floor -- the window-wide numbers in the source
       * summary are not bounded that way and stay exact either way.
       */}
      {scanTruncated ? (
        <p className="news-market-truncated" role="note">
          本页读取已达单页上限，×N 观测数按下限计；来源汇总仍是整窗口的准确计数。
        </p>
      ) : null}

      {hasMore ? (
        <div className="news-market-more">
          <ActionButton disabled={loadingMore} onClick={onLoadMore}>
            {loadingMore ? "正在加载" : "加载更多观测组"}
          </ActionButton>
          <small>已加载 {formatCount(groups.length)} 组；来源汇总描述的是整个窗口</small>
        </div>
      ) : null}
    </section>
  );
}

function GroupRow({ group, token }: { group: NewsMarketGroup; token: string }) {
  const [open, setOpen] = useState(false);
  const latest = group.latest;
  return (
    <article
      className="news-market-row"
      data-kind={group.market_kind}
      data-open={open || undefined}
    >
      <button
        aria-expanded={open}
        className="news-market-row-main"
        onClick={() => setOpen((value) => !value)}
        type="button"
      >
        <span className="news-market-row-head">
          <span className="news-market-time" title={displayTime(group.last_event_at_ms)}>
            {clockTime(group.last_event_at_ms)}
          </span>
          <span className="news-market-kind" title={marketKindTitle(group.market_kind)}>
            {marketKindLabel(group.market_kind)}
          </span>
          <b className="news-market-subject">{marketSubject(latest)}</b>
          {/* The run, not a rank: how many consecutive observations this one row stands for. */}
          <span className="news-market-count" title="本组连续观测条数">
            ×{formatCount(group.observation_count)}
          </span>
          <span className="news-market-window">
            {clockTime(group.first_event_at_ms)} → {clockTime(group.last_event_at_ms)}
          </span>
          <span className="news-market-spacer" />
          <ParseChip observation={latest} />
          <PushChip reason={group.notification_reason} status={group.notification_status} />
        </span>
        <span className="news-market-title">{latest.title}</span>
      </button>
      {open ? <GroupDetail itemId={latest.item_id} token={token} /> : null}
    </article>
  );
}

/** What the parser read. Never the push answer: they are different owners and different failures. */
function ParseChip({ observation }: { observation: NewsMarketObservation }) {
  return (
    <span className="news-market-flag" data-flag="parse" data-status={observation.parse_status}>
      <small>解析</small>
      <b>{marketParseLabel(observation.parse_status)}</b>
      {observation.parse_error ? <code>{observation.parse_error}</code> : null}
    </span>
  );
}

/**
 * What the notification owner did, in its own words.
 *
 * The status and the reason are server strings and are printed as written — the operator greps them, and a
 * Chinese gloss invented here would either rename one or silently swallow a status this build has not seen.
 */
function PushChip({ reason, status }: { reason: string; status: string }) {
  return (
    <span className="news-market-flag" data-flag="push">
      <small>推送</small>
      <b>{status || "—"}</b>
      {reason ? <code>{reason}</code> : null}
    </span>
  );
}

/**
 * What the notification owner recorded for this one observation.
 *
 * Server strings, printed as written, for the same reason `PushChip` prints them: the operator greps
 * these. A card that was sent shows the snapshot's own numbers — how many observations it spoke for and
 * how many attempts it took — because "sent" without them cannot be checked against the timeline below.
 */
function notificationTrace(item: NewsMarketItem): Array<[string, string]> {
  const delivery = item.notification_delivery;
  const entries: Array<[string, unknown]> = [
    ["notification_status", item.notification_status || "—"],
    ["notification_reason", item.notification_reason || "—"],
    ["trigger_reason", delivery?.trigger_reason],
    ["covered_count", delivery?.covered_count],
    ["attempts", delivery?.attempts],
    ["error", delivery?.error],
    ["receipt_provider", delivery?.receipt_provider],
    ["settled_at_ms", delivery?.settled_at_ms],
  ];
  return entries
    .filter(([, value]) => value !== null && value !== undefined && value !== "")
    .map(([key, value]) => [key, String(value)]);
}

/**
 * One expanded group, read by the newest observation's Item identity.
 *
 * The detail endpoint carries the group's retained timeline, so expanding is one request rather than one
 * per member, and the list itself never loads a payload a reader has not asked for.
 */
function GroupDetail({ itemId, token }: { itemId: string; token: string }) {
  const itemQuery = useNewsMarketItemWithToken(token, itemId);
  if (itemQuery.isLoading && !itemQuery.data) {
    return (
      <div className="news-market-detail">
        <PageState.Loading label="正在读取这一组观测" layout="inline" rows={3} />
      </div>
    );
  }
  if (itemQuery.isError && !itemQuery.data) {
    return (
      <div className="news-market-detail">
        <PageState.Error error={itemQuery.error} onRetry={() => void itemQuery.refetch()} />
      </div>
    );
  }
  const item = itemQuery.data;
  if (!item) return null;
  const params = Object.entries(item.provider_params);
  return (
    <div className="news-market-detail">
      <div className="news-market-detail-panel">
        <small className="news-market-detail-label">供应商原文</small>
        <code className="news-market-raw">{item.raw_first_line || item.observation.title}</code>
        {item.description ? <p className="news-market-description">{item.description}</p> : null}
        <small className="news-market-detail-label">PROVIDER_PARAMS</small>
        {params.length ? (
          <TraceList entries={params.map(([key, value]) => [key, String(value)])} />
        ) : (
          <p className="news-market-detail-empty">这条记录没有随附的供应商参数。</p>
        )}
      </div>

      <div className="news-market-detail-panel">
        <small className="news-market-detail-label">已入库字段</small>
        <TraceList entries={marketObservationTrace(item.observation)} />
        <small className="news-market-detail-label">推送</small>
        <TraceList entries={notificationTrace(item)} />
        {item.notification_delivery ? (
          <p className="news-market-detail-empty">
            这张卡覆盖 {item.notification_delivery.covered_count} 条观测，本条在其中。
          </p>
        ) : null}
      </div>

      <div className="news-market-detail-panel">
        <small className="news-market-detail-label">本组时间线 · {item.timeline.length}</small>
        <ol className="news-market-timeline">
          {item.timeline.map((observation) => (
            <li
              key={observation.item_id}
              data-current={observation.item_id === itemId || undefined}
            >
              <span
                className="news-market-timeline-time"
                title={displayTime(observation.event_at_ms)}
              >
                {clockTime(observation.event_at_ms)}
              </span>
              <span className="news-market-timeline-status" data-status={observation.parse_status}>
                {marketParseLabel(observation.parse_status)}
              </span>
              <span className="news-market-timeline-title">{observation.title}</span>
            </li>
          ))}
        </ol>
      </div>
    </div>
  );
}

function TraceList({ entries }: { entries: Array<[string, string]> }) {
  return (
    <dl className="news-market-trace">
      {entries.map(([key, value]) => (
        <div key={key}>
          <dt>{key}</dt>
          <dd>{value}</dd>
        </div>
      ))}
    </dl>
  );
}
