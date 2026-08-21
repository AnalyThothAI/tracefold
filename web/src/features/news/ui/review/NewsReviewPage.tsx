import { newsEventPath } from "@shared/routing/paths";
import { ActionButton } from "@shared/ui/ActionButton";
import { Bar } from "@shared/ui/Bar";
import { Card, CardNote } from "@shared/ui/Card";
import { Metric, MetricRow } from "@shared/ui/Metric";
import * as PageState from "@shared/ui/PageState";
import { Link, useSearchParams } from "react-router-dom";

import {
  NEWS_REVIEW_DEFAULT_HOURS,
  NEWS_REVIEW_HOURS,
  type NewsReview,
  type NewsReviewCoverage,
  type NewsReviewDirection,
  type NewsReviewMiss,
  useNewsReviewWithToken,
} from "../../api/newsQueries";
import { absoluteTime, clockTime, formatCount, labelCommand } from "../../model/newsLabels";
import { formatBps, priceTone } from "../../model/newsPrice";
import { NewsEmptyNote, NewsPageHeader, NewsPageShell, NewsTechnical } from "../chrome/NewsChrome";

import "./newsReview.css";

/**
 * 命中复盘 (#88): what the market actually did after the Events this pipeline judged.
 *
 * Coverage comes before accuracy, and every percentage is paired with the N it came from — a hit rate whose
 * denominator is invisible is the one number an operator must not be given. The browser renders server
 * values: it never computes a return, a rate, a coverage share or what "missing" means.
 *
 * The potential-miss table is a review *queue*. Price movement after an Event does not prove the Event caused
 * it or that it should have been pushed, so every row names the decision and the rule that produced it, and
 * nothing on this page writes a label — the button copies the same CLI command the feed's `X` does.
 */
export function NewsReviewPage({
  copy,
  token,
}: {
  copy: (text: string, note: string) => void;
  token: string;
}) {
  const [searchParams, setSearchParams] = useSearchParams();
  const hours = parseHours(searchParams.get("hours"));
  const query = useNewsReviewWithToken(token, hours);
  const review = query.data;
  return (
    <NewsPageShell archetype="scan" className="news-review-shell" label="命中复盘">
      <NewsPageHeader
        subtitle="以新闻发布时间为锚点，事件之后 1H / 4H 的实际涨跌。覆盖率在前，命中率在后。"
        title="命中复盘"
      >
        <label className="news-review-window">
          <span className="sr-only">复盘窗口</span>
          <select
            onChange={(event) => {
              const next = new URLSearchParams(searchParams);
              next.set("hours", event.target.value);
              setSearchParams(next, { replace: true });
            }}
            value={String(hours)}
          >
            {NEWS_REVIEW_HOURS.map((option) => (
              <option key={option} value={option}>
                {windowLabel(option)}
              </option>
            ))}
          </select>
        </label>
      </NewsPageHeader>

      {query.isLoading && !review ? (
        <PageState.Loading label="正在读取复盘数据" layout="panel" rows={4} />
      ) : null}
      {query.isError && !review ? (
        <PageState.Error error={query.error} onRetry={() => void query.refetch()} />
      ) : null}
      {review ? (
        <PageState.Stale updating={query.isFetching && !query.isLoading}>
          <div className="news-review-body">
            <Card aria-label="复盘覆盖率" flush hint="有多少事件真的算得出来" title="覆盖率">
              <Coverage coverage={review.coverage ?? []} />
            </Card>

            {/* Two short tables side by side; the long one gets the full width below them. */}
            <div className="news-review-grid">
              <Card
                aria-label="方向命中率"
                flush
                hint="中性与方向待定单独列出，不进命中率分母"
                title="方向命中"
              >
                <DirectionTable directions={review.directions ?? []} />
              </Card>
              <Card
                aria-label="影响程度校准"
                flush
                hint="模型给的量级 vs 实际绝对波动"
                title="影响程度校准"
              >
                <MagnitudeTable review={review} />
              </Card>
            </div>

            <Card
              aria-label="事件类型表现"
              hint="推送率的落差本身已经能指向阈值问题"
              title="各类事件的推送率"
            >
              <EventTypeBars review={review} />
            </Card>

            <Card
              aria-label="潜在漏推"
              flush
              hint="没有送达读者、且 1H 已完成的事件，按绝对涨跌排序"
              title="待核对的未推送事件"
            >
              <MissTable
                misses={review.potential_misses ?? []}
                onCopy={(eventId) =>
                  copy(labelCommand(eventId, "missed"), "已复制「漏推」标注命令")
                }
              />
            </Card>

            <NewsTechnical summary="度量口径">
              <section>
                <h4>口径</h4>
                <ul className="news-review-meta">
                  <li>
                    <span>指标版本</span>
                    <b>{review.meta.metric_version}</b>
                  </li>
                  <li>
                    <span>窗口</span>
                    <b>
                      {absoluteTime(review.meta.window_start_ms)} 起 ·{" "}
                      {windowLabel(review.meta.hours)}
                    </b>
                  </li>
                  <li>
                    <span>锚点</span>
                    <b>事件的 provider 发布时间，不是投递时间</b>
                  </li>
                  <li>
                    <span>算法</span>
                    <b>5m 已收盘 K 线，(pH / p0) - 1，不做前向填充</b>
                  </li>
                </ul>
              </section>
            </NewsTechnical>
          </div>
        </PageState.Stale>
      ) : null}
    </NewsPageShell>
  );
}

/** Coverage first: a hit rate over 12% of a window is a rumour, and the reader has to see that before the rate. */
function Coverage({ coverage }: { coverage: NewsReviewCoverage[] }) {
  if (!coverage.length) return <NewsEmptyNote>这个窗口里还没有可复盘的事件。</NewsEmptyNote>;
  return (
    <>
      <MetricRow columns={coverage.length} label="按时间窗覆盖率">
        {coverage.map((row) => (
          <Metric
            caption={row.horizon_zh}
            eyebrow={row.horizon.toUpperCase()}
            key={row.horizon}
            note={`已定价 ${formatCount(row.priced_n)} / ${formatCount(row.eligible_n)}`}
            tone={coverageTone(row.coverage_pct)}
            value={row.coverage_pct == null ? "—" : `${row.coverage_pct}%`}
          />
        ))}
      </MetricRow>
      {/* Why a horizon could not be priced, in the server's own words — never folded into the percentage. */}
      {coverage.some((row) => row.unavailable?.length) ? (
        <div className="news-review-reasons">
          {coverage.map((row) =>
            (row.unavailable ?? []).map((reason) => (
              <span key={`${row.horizon}-${reason.reason}`}>
                <small>{row.horizon_zh}</small>
                <em>{reason.reason_zh}</em>
                <b>{formatCount(reason.n)}</b>
              </span>
            )),
          )}
        </div>
      ) : null}
    </>
  );
}

/** Under a third of a window priced is a coverage problem, not a result: it is named, not coloured green. */
function coverageTone(pct: number | null | undefined) {
  if (pct == null) return "plain" as const;
  return pct < 30 ? ("caution" as const) : ("accent" as const);
}

function DirectionTable({ directions }: { directions: NewsReviewDirection[] }) {
  if (!directions.length) return <NewsEmptyNote>还没有可以打分的方向判断。</NewsEmptyNote>;
  return (
    // CSS grid does the layout and ARIA carries the semantics: a real `<table>` would need
    // `display: contents` on every row to lay out this way, which drops rows out of the accessibility tree
    // in some browsers — the opposite of what a table is for.
    <div className="news-review-table" role="table">
      <div className="news-review-head news-review-direction-row" role="row">
        <span role="columnheader">CALL</span>
        <span role="columnheader">窗口</span>
        <span role="columnheader">已定价</span>
        <span role="columnheader">命中</span>
        <span role="columnheader">覆盖率</span>
      </div>
      {directions.map((row) => (
        <div
          className="news-review-row news-review-direction-row"
          key={`${row.direction}-${row.horizon}`}
          role="row"
        >
          <span className="news-review-call" data-dir={row.direction} role="cell">
            {row.direction_zh || row.direction}
          </span>
          <span className="news-review-quiet" role="cell">
            {row.horizon_zh}
          </span>
          <b role="cell">{formatCount(row.priced_n)}</b>
          {row.scored && row.hit_pct != null ? (
            <b className="news-review-hit" role="cell">
              {row.hit_pct}%<small>N={row.priced_n}</small>
            </b>
          ) : (
            <span className="news-review-quiet" role="cell">
              {row.scored ? "样本不足" : "不计入"}
            </span>
          )}
          <span className="news-review-quiet" role="cell">
            {row.coverage_pct == null ? "—" : `${row.coverage_pct}%`}
          </span>
        </div>
      ))}
    </div>
  );
}

function EventTypeBars({ review }: { review: NewsReview }) {
  const rows = review.event_types ?? [];
  if (!rows.length) return <NewsEmptyNote>这个窗口还没有事件类型样本。</NewsEmptyNote>;
  // The gap between the best and worst push rate is the point; the lowest quartile takes the amber.
  const rates = rows.map((row) => row.pushed_pct ?? 0);
  const low = Math.min(...rates) + (Math.max(...rates) - Math.min(...rates)) * 0.25;
  return (
    <>
      <div className="news-review-types">
        {rows.map((row) => (
          <div className="news-review-type" key={row.event_type}>
            <span className="news-review-type-name">
              {row.event_type_zh || row.event_type}
              <small>n={formatCount(row.eligible_n)}</small>
            </span>
            <span className="news-review-type-bar">
              <Bar
                share={row.pushed_pct ?? 0}
                tone={(row.pushed_pct ?? 0) <= low ? "caution" : "neutral"}
              />
            </span>
            <b>{row.pushed_pct == null ? "—" : `${row.pushed_pct}%`}</b>
            <span className="news-review-quiet" data-tone={priceTone(row.median_1h_bps)}>
              {formatBps(row.median_1h_bps)}
            </span>
          </div>
        ))}
      </div>
      <CardNote>
        右列是该类事件 1H 的实际涨跌中位数；推送率低而波动大的类别就是阈值该动的地方。
      </CardNote>
    </>
  );
}

function MagnitudeTable({ review }: { review: NewsReview }) {
  const rows = review.magnitudes ?? [];
  if (!rows.length) return <NewsEmptyNote>这个窗口还没有量级样本。</NewsEmptyNote>;
  return (
    <div className="news-review-table" role="table">
      <div className="news-review-head news-review-magnitude-row" role="row">
        <span role="columnheader">量级</span>
        <span role="columnheader">事件</span>
        <span role="columnheader">占比</span>
        <span role="columnheader">1H 绝对中位</span>
        <span role="columnheader">4H 绝对中位</span>
        <span role="columnheader">覆盖率</span>
      </div>
      {rows.map((row) => (
        <div className="news-review-row news-review-magnitude-row" key={row.magnitude} role="row">
          <span className="news-review-name" role="cell">
            {row.magnitude_zh || row.magnitude}
          </span>
          <b role="cell">{formatCount(row.eligible_n)}</b>
          <span className="news-review-quiet" role="cell">
            {row.share_pct == null ? "—" : `${row.share_pct}%`}
          </span>
          <b role="cell">{formatBps(row.median_abs_1h_bps)}</b>
          <b role="cell">{formatBps(row.median_abs_4h_bps)}</b>
          <span className="news-review-quiet" role="cell">
            {row.coverage_1h_pct == null ? "—" : `${row.coverage_1h_pct}%`}
          </span>
        </div>
      ))}
    </div>
  );
}

function MissTable({
  misses,
  onCopy,
}: {
  misses: NewsReviewMiss[];
  onCopy: (eventId: string) => void;
}) {
  if (!misses.length) {
    return <NewsEmptyNote>这个窗口里，没有送达读者又出现明显波动的事件。</NewsEmptyNote>;
  }
  return (
    <>
      <div className="news-review-table" role="table">
        <div className="news-review-head news-review-miss-row" role="row">
          <span role="columnheader">TIME</span>
          <span role="columnheader">EVENT</span>
          <span role="columnheader">1H</span>
          <span role="columnheader">REASON</span>
          <span role="columnheader">ASSET</span>
          <span role="columnheader">LABEL</span>
        </div>
        {misses.map((miss) => (
          <div className="news-review-row news-review-miss-row" key={miss.event_id} role="row">
            <time
              dateTime={new Date(miss.opened_at_ms).toISOString()}
              role="cell"
              title={absoluteTime(miss.opened_at_ms)}
            >
              {clockTime(miss.opened_at_ms)}
            </time>
            <Link className="news-review-miss-title" role="cell" to={newsEventPath(miss.event_id)}>
              {miss.headline_zh || miss.leader_title}
            </Link>
            <b data-tone={priceTone(miss.return_1h_bps)} role="cell">
              {formatBps(miss.return_1h_bps)}
            </b>
            {/* The decision and the rule that produced it, both in words: movement is never the reason. */}
            <span className="news-review-miss-rule" role="cell">
              <span>{miss.decision_zh}</span>
              {miss.throttled_by_zh || miss.override_rule_zh ? (
                <small>{miss.throttled_by_zh || miss.override_rule_zh}</small>
              ) : null}
            </span>
            <span className="news-review-miss-assets" role="cell">
              {miss.assets?.length ? (
                miss.assets.slice(0, 2).map((asset) => (
                  <code key={asset.symbol}>
                    {asset.venue
                      ? `${asset.venue}:${asset.venue_symbol ?? asset.symbol}`
                      : asset.symbol}
                    <b data-tone={priceTone(asset.return_1h_bps)}>
                      {formatBps(asset.return_1h_bps)}
                    </b>
                  </code>
                ))
              ) : (
                <code>—</code>
              )}
            </span>
            <span role="cell">
              <ActionButton onClick={() => onCopy(miss.event_id)} size="sm">
                标为漏推
              </ActionButton>
            </span>
          </div>
        ))}
      </div>
      <CardNote>
        标注不走写接口：按钮复制对应的 CLI 命令，语义与详情页一致。涨跌不能证明因果。
      </CardNote>
    </>
  );
}

function parseHours(raw: string | null): number {
  const value = Number(raw);
  return NEWS_REVIEW_HOURS.includes(value) ? value : NEWS_REVIEW_DEFAULT_HOURS;
}

function windowLabel(hours: number): string {
  if (hours % 24 === 0) return `最近 ${hours / 24} 天`;
  return `最近 ${hours} 小时`;
}
