import { newsEventPath } from "@shared/routing/paths";
import { Card } from "@shared/ui/Card";
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
import { absoluteTime, formatCount, labelCommand } from "../../model/newsLabels";
import { formatBps, priceTone } from "../../model/newsPrice";
import { useNewsToast } from "../../state/useNewsToast";
import { NewsEmptyNote, NewsPageHeader, NewsPageShell, NewsTechnical } from "../chrome/NewsChrome";
import { NewsToast } from "../chrome/NewsToast";

import "./newsReview.css";

/**
 * 命中复盘 (#88): what the market actually did after the Events this pipeline judged.
 *
 * Coverage comes before accuracy, and every percentage is paired with the N it came from — a hit rate whose
 * denominator is invisible is the one number an operator must not be given. The browser renders server
 * values: it never computes a return, a rate, a coverage share or what "missing" means.
 *
 * The potential-miss table is a review queue. Price movement after an Event does not prove the Event caused
 * it or that it should have been pushed, so every row names the decision and the rule that produced it, and
 * nothing on this page writes a label.
 */
export function NewsReviewPage({ token }: { token: string }) {
  const [searchParams, setSearchParams] = useSearchParams();
  const hours = parseHours(searchParams.get("hours"));
  const query = useNewsReviewWithToken(token, hours);
  const review = query.data;
  const toast = useNewsToast();
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
        <PageState.Loading layout="panel" rows={4} label="正在读取复盘数据" />
      ) : null}
      {query.isError && !review ? (
        <PageState.Error error={query.error} onRetry={() => void query.refetch()} />
      ) : null}
      {review ? (
        <PageState.Stale updating={query.isFetching && !query.isLoading}>
          <div className="news-review-body">
            <Card title="覆盖率" hint="有多少事件真的算得出来" aria-label="复盘覆盖率">
              <CoverageGrid coverage={review.coverage ?? []} />
            </Card>

            <Card
              title="方向命中"
              hint="只统计非降级、且该 horizon 已完成的判断；中性与方向待定单独列出，不进命中率分母"
              aria-label="方向命中率"
            >
              <DirectionTable directions={review.directions ?? []} />
            </Card>

            <div className="news-review-grid">
              <Card
                title="影响程度校准"
                hint="模型给的量级 vs 实际绝对波动"
                aria-label="影响程度校准"
              >
                <MagnitudeTable review={review} />
              </Card>
              <Card title="事件类型表现" hint="推送率、收益分布与覆盖率" aria-label="事件类型表现">
                <EventTypeTable review={review} />
              </Card>
            </div>

            <Card
              title="潜在漏推"
              hint="没有送达读者、且 1H 已完成的事件，按绝对涨跌排序。涨跌不能证明因果。"
              aria-label="潜在漏推"
            >
              <MissTable
                misses={review.potential_misses ?? []}
                onCopy={(eventId) =>
                  toast.copy(labelCommand(eventId, "missed"), "已复制「漏推」标注命令")
                }
              />
            </Card>

            <NewsTechnical summary="度量口径">
              <dl className="news-review-meta">
                <div>
                  <dt>指标版本</dt>
                  <dd>{review.meta.metric_version}</dd>
                </div>
                <div>
                  <dt>窗口</dt>
                  <dd>
                    {absoluteTime(review.meta.window_start_ms)} 起 ·{" "}
                    {windowLabel(review.meta.hours)}
                  </dd>
                </div>
                <div>
                  <dt>锚点</dt>
                  <dd>事件的 provider 发布时间，不是投递时间</dd>
                </div>
                <div>
                  <dt>口径</dt>
                  <dd>5m 已收盘 K 线，(pH / p0) - 1，不做前向填充</dd>
                </div>
              </dl>
            </NewsTechnical>
          </div>
        </PageState.Stale>
      ) : null}
      <NewsToast message={toast.message} />
    </NewsPageShell>
  );
}

function CoverageGrid({ coverage }: { coverage: NewsReviewCoverage[] }) {
  if (!coverage.length) return <NewsEmptyNote>这个窗口里还没有可复盘的事件。</NewsEmptyNote>;
  return (
    <div className="news-review-coverage">
      {coverage.map((row) => (
        <article key={row.horizon}>
          <header>
            <span className="news-review-coverage-title">{row.horizon_zh}</span>
            <span className="news-review-coverage-pct">
              {row.coverage_pct == null ? "—" : `${row.coverage_pct}%`}
            </span>
          </header>
          <div aria-hidden className="news-review-bar">
            <span style={{ width: `${Math.min(100, row.coverage_pct ?? 0)}%` }} />
          </div>
          <p className="news-review-coverage-line">
            可评估 {formatCount(row.eligible_n)} · 已定价 {formatCount(row.priced_n)}
            {row.no_primary_n ? ` · 无主标的 ${formatCount(row.no_primary_n)}` : ""}
            {row.degraded_n ? ` · 降级 ${formatCount(row.degraded_n)}` : ""}
          </p>
          {row.unavailable?.length ? (
            <ul className="news-review-reasons">
              {row.unavailable.map((reason) => (
                <li key={reason.reason}>
                  <span>{reason.reason_zh}</span>
                  <b>{formatCount(reason.n)}</b>
                </li>
              ))}
            </ul>
          ) : null}
        </article>
      ))}
    </div>
  );
}

function DirectionTable({ directions }: { directions: NewsReviewDirection[] }) {
  if (!directions.length) return <NewsEmptyNote>还没有可以打分的方向判断。</NewsEmptyNote>;
  return (
    <div className="news-review-table-wrap">
      <table className="news-review-table">
        <thead>
          <tr>
            <th scope="col">方向</th>
            <th scope="col">窗口</th>
            <th className="news-review-n" scope="col">
              可评估
            </th>
            <th className="news-review-n" scope="col">
              已定价
            </th>
            <th className="news-review-n" scope="col">
              命中
            </th>
            <th className="news-review-n" scope="col">
              中位涨跌
            </th>
            <th className="news-review-n" scope="col">
              覆盖率
            </th>
          </tr>
        </thead>
        <tbody>
          {directions.map((row) => (
            <tr data-scored={row.scored || undefined} key={`${row.direction}-${row.horizon}`}>
              <th scope="row">{row.direction_zh || row.direction}</th>
              <td>{row.horizon_zh}</td>
              <td className="news-review-n">{formatCount(row.eligible_n)}</td>
              <td className="news-review-n">{formatCount(row.priced_n)}</td>
              <td className="news-review-n">
                {row.scored && row.hit_pct != null ? (
                  <span className="news-review-hit">
                    {row.hit_pct}% <small>N={row.priced_n}</small>
                  </span>
                ) : (
                  <span className="news-review-muted">{row.scored ? "样本不足" : "不计入"}</span>
                )}
              </td>
              <td className="news-review-n" data-tone={priceTone(row.median_bps)}>
                {formatBps(row.median_bps)}
              </td>
              <td className="news-review-n">
                {row.coverage_pct == null ? "—" : `${row.coverage_pct}%`}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function MagnitudeTable({ review }: { review: NewsReview }) {
  const rows = review.magnitudes ?? [];
  if (!rows.length) return <NewsEmptyNote>这个窗口还没有量级样本。</NewsEmptyNote>;
  return (
    <div className="news-review-table-wrap">
      <table className="news-review-table">
        <thead>
          <tr>
            <th scope="col">量级</th>
            <th className="news-review-n" scope="col">
              事件
            </th>
            <th className="news-review-n" scope="col">
              占比
            </th>
            <th className="news-review-n" scope="col">
              1H 绝对中位
            </th>
            <th className="news-review-n" scope="col">
              4H 绝对中位
            </th>
            <th className="news-review-n" scope="col">
              覆盖率
            </th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.magnitude}>
              <th scope="row">
                {row.magnitude} · {row.magnitude_zh}
              </th>
              <td className="news-review-n">{formatCount(row.eligible_n)}</td>
              <td className="news-review-n">{row.share_pct == null ? "—" : `${row.share_pct}%`}</td>
              <td className="news-review-n">{formatBps(row.median_abs_1h_bps)}</td>
              <td className="news-review-n">{formatBps(row.median_abs_4h_bps)}</td>
              <td className="news-review-n">
                {row.coverage_1h_pct == null ? "—" : `${row.coverage_1h_pct}%`}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function EventTypeTable({ review }: { review: NewsReview }) {
  const rows = review.event_types ?? [];
  if (!rows.length) return <NewsEmptyNote>这个窗口还没有事件类型样本。</NewsEmptyNote>;
  return (
    <div className="news-review-table-wrap">
      <table className="news-review-table">
        <thead>
          <tr>
            <th scope="col">类型</th>
            <th className="news-review-n" scope="col">
              事件
            </th>
            <th className="news-review-n" scope="col">
              推送率
            </th>
            <th className="news-review-n" scope="col">
              1H 中位
            </th>
            <th className="news-review-n" scope="col">
              1H 绝对中位
            </th>
            <th className="news-review-n" scope="col">
              覆盖率
            </th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.event_type}>
              <th scope="row">{row.event_type_zh || row.event_type}</th>
              <td className="news-review-n">{formatCount(row.eligible_n)}</td>
              <td className="news-review-n">
                {row.pushed_pct == null ? "—" : `${row.pushed_pct}%`}
                <small> ({formatCount(row.pushed_n)})</small>
              </td>
              <td className="news-review-n" data-tone={priceTone(row.median_1h_bps)}>
                {formatBps(row.median_1h_bps)}
              </td>
              <td className="news-review-n">{formatBps(row.median_abs_1h_bps)}</td>
              <td className="news-review-n">
                {row.coverage_1h_pct == null ? "—" : `${row.coverage_1h_pct}%`}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
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
    <ul className="news-review-misses">
      {misses.map((miss) => (
        <li key={miss.event_id}>
          <div className="news-review-miss-head">
            <span className="news-review-miss-move" data-tone={priceTone(miss.return_1h_bps)}>
              {formatBps(miss.return_1h_bps)}
            </span>
            <Link className="news-review-miss-title" to={newsEventPath(miss.event_id)}>
              {miss.headline_zh || miss.leader_title}
            </Link>
          </div>
          <p className="news-review-miss-meta">
            <span className="news-review-miss-decision">{miss.decision_zh}</span>
            {miss.throttled_by_zh ? <span>{miss.throttled_by_zh}</span> : null}
            {miss.override_rule_zh ? <span>{miss.override_rule_zh}</span> : null}
            {miss.direction_zh ? <span>{miss.direction_zh}</span> : null}
            {miss.magnitude_zh ? <span>{miss.magnitude_zh}</span> : null}
            {miss.event_type_zh ? <span>{miss.event_type_zh}</span> : null}
            <span>4H {formatBps(miss.return_4h_bps)}</span>
            <time dateTime={new Date(miss.opened_at_ms).toISOString()}>
              {absoluteTime(miss.opened_at_ms)}
            </time>
          </p>
          {miss.assets?.length ? (
            <p className="news-review-miss-assets">
              {miss.assets.map((asset) => (
                <code key={asset.symbol}>
                  {asset.venue
                    ? `${asset.venue}:${asset.venue_symbol ?? asset.symbol}`
                    : asset.symbol}
                  <b data-tone={priceTone(asset.return_1h_bps)}>{formatBps(asset.return_1h_bps)}</b>
                </code>
              ))}
            </p>
          ) : null}
          <button
            className="news-review-miss-label"
            onClick={() => onCopy(miss.event_id)}
            type="button"
          >
            复制「漏推」标注命令
          </button>
        </li>
      ))}
    </ul>
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
