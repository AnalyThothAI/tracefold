import { useMediaQuery } from "@shared/hooks/useMediaQuery";
import { ActionButton } from "@shared/ui/ActionButton";
import { Drawer } from "@shared/ui/Drawer";
import { EmptyNote } from "@shared/ui/EmptyNote";
import * as PageState from "@shared/ui/PageState";
import { useRef } from "react";
import { Link, useSearchParams } from "react-router-dom";

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
import { formatPrice } from "../../model/newsPrice";
import { walletTokenAge } from "../../model/walletFacts";

import { NewsOiTimeline } from "./NewsOiTimeline";
import { MarketObservationMetrics, OiEvidence } from "./OiEvidence";
import "./newsMarketGroupTable.css";

/** The list stays anchored while one URL-selected observation is read beside it. */
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
  const [params, setParams] = useSearchParams();
  const selectedItem = params.get("item");
  const wide = useMediaQuery("(min-width: 1100px)");
  const opener = useRef<HTMLElement | null>(null);
  const select = (id: string | null) => {
    if (id)
      opener.current =
        document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const next = new URLSearchParams(params);
    if (id) next.set("item", id);
    else next.delete("item");
    setParams(next, { replace: true });
  };
  return (
    <div className="news-market-workbench">
      <section aria-label="市场观测" className="news-market-panel">
        <header className="news-market-list-heading">
          <h2>市场观察</h2>
          <span>来源事实 · 按口径阅读</span>
        </header>
        <div className="news-market-toolbar">
          <div aria-label="按市场类型筛选" className="news-market-kinds" role="group">
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
        </div>
        <p className="news-market-window-caption">
          {kinds.length ? `${kinds.length} / ${NEWS_MARKET_KINDS.length} 类` : "全部类型"} ·{" "}
          {displayTime(filters.from_ms)} → {displayTime(filters.to_ms)}
        </p>
        {groups.length === 0 ? (
          <EmptyNote>这个窗口里没有符合当前筛选的市场观测。</EmptyNote>
        ) : (
          <div className="news-market-rows">
            {groups.map((group) => (
              <GroupRow
                group={group}
                key={`${group.group_key}:${group.latest.item_id}`}
                open={selectedItem === group.latest.item_id}
                onSelect={() =>
                  select(selectedItem === group.latest.item_id ? null : group.latest.item_id)
                }
              />
            ))}
          </div>
        )}
        {scanTruncated ? (
          <p className="news-market-truncated" role="note">
            本页读取已达单页上限，×N 观测数按下限计；来源汇总仍是整窗口的准确计数。
          </p>
        ) : null}
        <div className="news-market-more">
          {hasMore ? (
            <ActionButton disabled={loadingMore} onClick={onLoadMore}>
              {loadingMore ? "正在加载" : "加载更多观测组"}
            </ActionButton>
          ) : null}
          <small>已加载 {formatCount(groups.length)} 组；来源汇总描述的是整个窗口</small>
        </div>
      </section>
      {selectedItem ? (
        <Drawer
          title="市场观察依据"
          open
          inline={wide}
          modal={false}
          flush
          width={520}
          restoreFocusTo={opener.current}
          onOpenChange={(open) => {
            if (!open) select(null);
          }}
          actions={
            <ActionButton size="sm" onClick={() => select(null)}>
              关闭依据
            </ActionButton>
          }
        >
          <GroupDetail itemId={selectedItem} token={token} />
        </Drawer>
      ) : (
        <aside className="news-market-detail-placeholder" aria-label="研究依据">
          <span>RESEARCH / EVIDENCE</span>
          <h2>从一条观察开始</h2>
          <p>选择左侧观察，核对变化、测量口径与原始依据，再查看它对应的策略判定。</p>
          <small>观察事实与策略结论分别记录。</small>
        </aside>
      )}
    </div>
  );
}

function GroupRow({
  group,
  open,
  onSelect,
}: {
  group: NewsMarketGroup;
  open: boolean;
  onSelect: () => void;
}) {
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
        onClick={onSelect}
        type="button"
      >
        <span className="news-market-row-head">
          <b className="news-market-subject">{marketSubject(latest)}</b>
          <span className="news-market-kind" title={marketKindTitle(group.market_kind)}>
            {marketKindLabel(group.market_kind)}
            {latest.wallet_snapshot ? " · 集中净买入" : ""}
          </span>
          <span className="news-market-count" title="本组连续观测条数">
            ×{formatCount(group.observation_count)}
          </span>
          <span className="news-market-spacer" />
          <span className="news-market-time" title={displayTime(group.last_event_at_ms)}>
            {clockTime(group.last_event_at_ms)}
          </span>
        </span>
        <MarketObservationMetrics observation={latest} />
        <span className="news-market-row-foot">
          <span className="news-market-window">
            {clockTime(group.first_event_at_ms)} → {clockTime(group.last_event_at_ms)}
          </span>
          <small>
            {open ? "收起依据" : "查看依据"}
            <span aria-hidden> ↗</span>
          </small>
        </span>
        {latest.parse_status === "raw" ? <ParseChip observation={latest} /> : null}
      </button>
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
 * The card as it was actually sent, and the observations it spoke for.
 *
 * This is what an operator holds against the message their channel received: the snapshot is frozen at
 * the first attempt and is never re-rendered, so a difference between it and the received card is a
 * real difference rather than a re-render. The covered ids are the Items that carry this card's key —
 * "which observations did this one message answer for" — and the count beside them is the card's own,
 * which is the honest total when the id list is capped.
 */
function SentSnapshot({
  covered,
  delivery,
}: {
  covered: readonly string[];
  delivery: NonNullable<NewsMarketItem["notification_delivery"]>;
}) {
  const lines = cardLines(delivery.card);
  return (
    <>
      <small className="news-market-detail-label">发送快照 · {delivery.covered_count} 条观测</small>
      {lines.length ? (
        <div className="news-market-card-snapshot">
          {lines.map((line, index) => (
            // The snapshot is a frozen payload with no identity of its own; a line's position in it
            // is the only key there is, and it never reorders because it is never rewritten.
            <p key={`${index}-${line}`}>{line}</p>
          ))}
        </div>
      ) : (
        <p className="news-market-detail-empty">这张卡还没有开始发送，快照要到首次尝试才冻结。</p>
      )}
      <small className="news-market-detail-label">覆盖观测 · {covered.length}</small>
      {covered.length ? (
        <ul className="news-market-covered">
          {covered.map((itemId) => (
            <li key={itemId}>
              <code>{itemId}</code>
            </li>
          ))}
        </ul>
      ) : (
        <p className="news-market-detail-empty">这张卡还没有认领任何观测。</p>
      )}
    </>
  );
}

/**
 * The card's own text, read out of the frozen payload rather than re-derived.
 *
 * The header title and the markdown block are the two parts a channel actually renders; anything else
 * in the payload is transport. A snapshot this build cannot read prints nothing rather than a guess.
 */
function cardLines(card: Record<string, unknown> | undefined): string[] {
  if (!card) return [];
  const header = card.header as { title?: { content?: unknown } } | undefined;
  const title = String(header?.title?.content ?? "").trim();
  const elements = Array.isArray(card.elements) ? card.elements : [];
  const body = elements
    .filter(
      (element): element is { tag: string; content: unknown } =>
        typeof element === "object" &&
        element !== null &&
        (element as { tag?: string }).tag === "markdown",
    )
    .flatMap((element) => String(element.content ?? "").split("\n"));
  return [title, ...body].map((line) => line.trim()).filter(Boolean);
}

/**
 * One expanded group, read by the newest observation's Item identity.
 *
 * The detail endpoint carries the group's retained timeline, so expanding is one request rather than one
 * per member, and the list itself never loads a payload a reader has not asked for.
 */
export function GroupDetail({ itemId, token }: { itemId: string; token: string }) {
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
      <header className="news-market-evidence-heading">
        <span>{marketKindLabel(item.observation.market_kind)} · 已存储观察</span>
        <h2>{marketSubject(item.observation)}</h2>
        <small>
          {displayTime(item.observation.event_at_ms)} ·{" "}
          {item.observation.source_venue ?? "场所未确认"}
        </small>
      </header>
      <OiEvidence observation={item.observation} />
      {item.observation.market_kind !== "oi" ? (
        <div className="news-market-detail-panel">
          <MarketObservationMetrics observation={item.observation} />
        </div>
      ) : null}
      <NewsOiTimeline observations={item.timeline} />
      <details className="news-market-raw-evidence">
        <summary>原始记录、解析与通知依据</summary>
        <div className="news-market-detail-panel">
          <ParseChip observation={item.observation} />
          <PushChip reason={item.notification_reason} status={item.notification_status} />
          <WalletEventEvidence observation={item.observation} />
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
            <SentSnapshot
              covered={item.notification_covered_item_ids ?? []}
              delivery={item.notification_delivery}
            />
          ) : null}
        </div>
      </details>
      <div className="news-market-detail-panel">
        <small className="news-market-detail-label">
          本组离散观察 · 最多 200 条 · {item.timeline.length}
        </small>
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
              <MarketObservationMetrics observation={observation} />
            </li>
          ))}
        </ol>
      </div>
    </div>
  );
}

function WalletEventEvidence({ observation }: { observation: NewsMarketObservation }) {
  const snapshot = observation.wallet_snapshot;
  if (!snapshot) return null;
  const window = snapshot.window;
  return (
    <>
      <small className="news-market-detail-label">集中净买入事件</small>
      <TraceList
        entries={[
          ["触发窗口", "30 分钟"],
          ["合格地址", `${window.qualified_n} / ${window.required_n}`],
          ["合格地址净买入", formatPrice(window.net_usd)],
          ["代币年龄", walletTokenAge(snapshot)],
          ["代币合约", snapshot.token],
        ]}
      />
      <Link to={`/news/wallets?episode=${observation.item_id}`}>查看集中净买入事件</Link>
    </>
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
