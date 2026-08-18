import { newsPath } from "@shared/routing/paths";
import * as PageState from "@shared/ui/PageState";
import { Link } from "react-router-dom";

import "./news.css";
import "./newsOutcome.css";
import "./newsStatus.css";
import { NewsSectionTabs } from "./NewsSectionTabs";
import {
  HEALTH_ITEM_KEYS,
  absoluteTime,
  formatCount,
  healthItemTitle,
  healthLevelLabel,
  healthTone,
  optionalDuration,
  optionalTime,
  percent,
  reasonStageLabel,
} from "./newsLabels";
import {
  type NewsHealthItem,
  type NewsReasonCount,
  type NewsStatus,
  useNewsStatusWithToken,
} from "./useNewsPage";

const REASON_STAGE_ORDER = ["push", "throttle", "drop", "gate", "degraded"] as const;

export function NewsStatusPage({ token }: { token: string }) {
  const query = useNewsStatusWithToken(token);
  const status = query.data;
  return (
    <section
      aria-label="新闻流水线状态"
      className="news-panel news-status-shell"
      data-page-archetype="scan"
    >
      <header className="news-page-header">
        <div className="news-heading-copy">
          <h1>流水线状态</h1>
          <p>四个环节的健康度、过去 24 小时的去向，以及当前控制状态。</p>
        </div>
        <div className="news-status-header-side">
          <NewsSectionTabs active="status" />
          {status ? <OverallPill status={status} /> : null}
        </div>
      </header>

      {query.isLoading && !status ? (
        <PageState.Loading layout="panel" rows={4} label="正在读取流水线状态" />
      ) : null}
      {query.isError && !status ? (
        <PageState.Error error={query.error} onRetry={() => void query.refetch()} />
      ) : null}
      {status && !status.health ? (
        <PageState.Error
          error={new Error("状态接口版本过旧，请刷新页面")}
          onRetry={() => void query.refetch()}
        />
      ) : null}
      {status?.health ? (
        <PageState.Stale updating={query.isFetching}>
          <div className="news-status-body">
            <div className="news-health-grid">
              {HEALTH_ITEM_KEYS.map((key) => (
                <HealthCard item={status.health[key]} key={key} title={healthItemTitle(key)}>
                  <HealthNumbers keyName={key} status={status} />
                </HealthCard>
              ))}
            </div>

            <div className="news-status-grid">
              <section aria-label="过去 24 小时漏斗" className="news-card">
                <div className="news-card-header">
                  <h2>过去 24 小时去向</h2>
                  <small>点击一层可跳到对应事件</small>
                </div>
                <Funnel status={status} />
              </section>
              <section aria-label="拦截与推送原因" className="news-card">
                <div className="news-card-header">
                  <h2>原因排行</h2>
                  <small>过去 24 小时，按事件数</small>
                </div>
                <ReasonBars reasons={status.reasons_24h ?? []} />
              </section>
            </div>

            <div className="news-status-grid">
              <section aria-label="控制状态" className="news-card">
                <div className="news-card-header">
                  <h2>控制</h2>
                  <small>用 tracefold news control 修改</small>
                </div>
                <ControlView status={status} />
              </section>
              <section aria-label="关注列表与策略" className="news-card">
                <div className="news-card-header">
                  <h2>关注列表与策略</h2>
                </div>
                <WatchAndStrategies status={status} />
              </section>
            </div>

            <TechnicalMetrics status={status} />
          </div>
        </PageState.Stale>
      ) : null}
    </section>
  );
}

function OverallPill({ status }: { status: NewsStatus }) {
  const level = status.health.overall;
  return (
    <span className="news-health-pill news-toned" data-tone={healthTone(level)} role="status">
      <span aria-hidden className="news-outcome-dot" />
      <b>总体{healthLevelLabel(level)}</b>
      <small>更新于 {absoluteTime(status.measured_at_ms).slice(11)}</small>
    </span>
  );
}

function HealthCard({
  children,
  item,
  title,
}: {
  children?: React.ReactNode;
  item: NewsHealthItem;
  title: string;
}) {
  return (
    <article
      className="news-health-card news-toned"
      data-level={item.level}
      data-tone={healthTone(item.level)}
    >
      <header>
        <span className="news-health-card-title">{title}</span>
        <span className="news-health-card-level">
          <span aria-hidden className="news-outcome-dot" />
          {healthLevelLabel(item.level)}
        </span>
      </header>
      <p className="news-health-card-summary">{item.summary_zh}</p>
      {item.detail_zh ? <p className="news-health-card-detail">{item.detail_zh}</p> : null}
      {children}
    </article>
  );
}

function HealthNumbers({
  keyName,
  status,
}: {
  keyName: (typeof HEALTH_ITEM_KEYS)[number];
  status: NewsStatus;
}) {
  const cells: Array<[string, string]> =
    keyName === "ingest"
      ? [
          ["最近一帧", optionalTime(status.ingest.last_frame_at_ms).slice(11) || "尚无"],
          ["最近 1 小时事件", formatCount(status.pipeline.events_1h)],
        ]
      : keyName === "broker"
        ? [
            ["审稿队列", formatCount(status.broker.queues?.["news.triage"]?.messages ?? 0)],
            ["投递队列", formatCount(status.broker.queues?.["news.deliver"]?.messages ?? 0)],
          ]
        : keyName === "model"
          ? [
              ["24h 判断", formatCount(status.pipeline.triage_24h)],
              ["延迟 p95", optionalDuration(status.pipeline.triage_p95_ms)],
            ]
          : [
              ["24h 送达", formatCount(status.delivery.sent_24h)],
              ["每小时上限", formatCount(status.delivery.hourly_cap)],
            ];
  return (
    <dl className="news-health-card-numbers">
      {cells.map(([label, value]) => (
        <div key={label}>
          <dt>{label}</dt>
          <dd>{value}</dd>
        </div>
      ))}
    </dl>
  );
}

function Funnel({ status }: { status: NewsStatus }) {
  const funnel = status.funnel_24h;
  const max = Math.max(1, funnel.received);
  const layers = [
    {
      label: "收到",
      value: funnel.received,
      hint: `最近 1 小时 ${formatCount(funnel.received_1h)}`,
      to: newsPath(),
    },
    {
      label: "候选（送审）",
      value: funnel.candidates,
      hint: percent(funnel.candidates, funnel.received),
      to: null,
    },
    {
      label: "模型判断",
      value: funnel.triaged,
      hint: percent(funnel.triaged, funnel.received),
      to: null,
    },
    {
      label: "决定推送",
      value: funnel.decided_push,
      hint: percent(funnel.decided_push, funnel.received),
      to: `${newsPath()}?outcome=pushed`,
    },
    {
      label: "已送达",
      value: funnel.delivered,
      hint: `最近 1 小时 ${formatCount(funnel.delivered_1h)}`,
      to: `${newsPath()}?outcome=pushed`,
    },
  ];
  return (
    <ol className="news-funnel">
      {layers.map((layer) => {
        const width = `${Math.max(4, Math.round((layer.value / max) * 100))}%`;
        const content = (
          <>
            <span className="news-funnel-label">{layer.label}</span>
            <span className="news-funnel-bar" style={{ width }} />
            <b>{formatCount(layer.value)}</b>
            <small>{layer.hint}</small>
          </>
        );
        return (
          <li key={layer.label}>
            {layer.to ? (
              <Link className="news-funnel-row" to={layer.to}>
                {content}
              </Link>
            ) : (
              <span className="news-funnel-row">{content}</span>
            )}
          </li>
        );
      })}
    </ol>
  );
}

function ReasonBars({ reasons }: { reasons: NewsReasonCount[] }) {
  if (!reasons.length) return <p className="news-detail-empty">过去 24 小时没有记录。</p>;
  const max = Math.max(1, ...reasons.map((reason) => reason.count));
  const groups = REASON_STAGE_ORDER.map((stage) => ({
    stage,
    rows: reasons.filter((reason) => reason.stage === stage).slice(0, 6),
  })).filter((group) => group.rows.length);
  return (
    <div className="news-reason-groups">
      {groups.map((group) => (
        <section className="news-reason-group" data-stage={group.stage} key={group.stage}>
          <h3>{reasonStageLabel(group.stage)}</h3>
          <ul>
            {group.rows.map((reason) => (
              <li key={`${reason.stage}-${reason.key}`} title={reason.key}>
                <span className="news-reason-label">{reason.label_zh}</span>
                <span className="news-reason-bar-track">
                  <span
                    className="news-reason-bar"
                    style={{ width: `${Math.max(3, Math.round((reason.count / max) * 100))}%` }}
                  />
                </span>
                <b>{formatCount(reason.count)}</b>
              </li>
            ))}
          </ul>
        </section>
      ))}
    </div>
  );
}

function ControlView({ status }: { status: NewsStatus }) {
  const mutes = status.control.mutes as Array<Record<string, unknown>>;
  return (
    <div className="news-control">
      <p className="news-control-row">
        <span>推送</span>
        <b data-paused={status.control.paused || undefined}>
          {status.control.paused ? "已暂停" : "运行中"}
        </b>
      </p>
      {mutes.length ? (
        <table className="news-mute-table">
          <thead>
            <tr>
              <th>类型</th>
              <th>对象</th>
              <th>到期</th>
            </tr>
          </thead>
          <tbody>
            {mutes.map((mute, index) => (
              <tr key={index}>
                <td>{String(mute.kind ?? "")}</td>
                <td>{String(mute.key ?? mute.symbol ?? mute.storyline_key ?? "")}</td>
                <td>{typeof mute.until_ms === "number" ? absoluteTime(mute.until_ms) : "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <p className="news-detail-empty">没有生效中的静音。</p>
      )}
    </div>
  );
}

function WatchAndStrategies({ status }: { status: NewsStatus }) {
  const warnings = status.ingest.strategy_warnings ?? [];
  const watchlist = status.watchlist ?? [];
  const strategies = status.ingest.configured_strategy_ids ?? [];
  return (
    <div className="news-watch">
      <p className="news-control-row">
        <span>关注列表</span>
        <span className="news-chip-row">
          {watchlist.length ? (
            watchlist.map((symbol) => <code key={symbol}>{symbol}</code>)
          ) : (
            <em>未配置</em>
          )}
        </span>
      </p>
      <p className="news-control-row">
        <span>Strategy</span>
        <span>
          已配置 {formatCount(strategies.length)} 个
          {status.ingest.provider_enabled_strategy_ids
            ? ` · provider 已启用 ${formatCount(status.ingest.provider_enabled_strategy_ids.length)} 个`
            : ""}
        </span>
      </p>
      {warnings.length ? (
        <ul className="news-warning-list">
          {warnings.map((warning) => (
            <li key={warning}>{warning}</li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

function TechnicalMetrics({ status }: { status: NewsStatus }) {
  const queues = Object.entries(status.broker.queues ?? {});
  const incidents = status.ingest.open_incidents ?? [];
  return (
    <details className="news-technical">
      <summary>技术指标（延迟分位、队列深度、事故、原始计数）</summary>
      <div>
        <section>
          <h4>流水线</h4>
          <dl className="news-kv">
            <KV k="state" v={status.state} />
            <KV k="workers_state" v={status.workers_state ?? "—"} />
            <KV k="triage_model" v={status.pipeline.triage_model ?? "—"} />
            <KV k="triage_p50_ms" v={optionalDuration(status.pipeline.triage_p50_ms)} />
            <KV k="triage_p95_ms" v={optionalDuration(status.pipeline.triage_p95_ms)} />
            <KV k="queue_lag_p95_ms" v={optionalDuration(status.pipeline.queue_lag_p95_ms)} />
            <KV k="e2e_p95_ms" v={optionalDuration(status.delivery.e2e_p95_ms)} />
            <KV k="events_24h" v={status.pipeline.events_24h} />
            <KV k="candidates_24h" v={status.pipeline.candidates_24h} />
            <KV k="triage_24h" v={status.pipeline.triage_24h} />
            <KV k="triage_degraded_24h" v={status.pipeline.triage_degraded_24h} />
            <KV k="throttled_24h" v={status.pipeline.throttled_24h} />
            <KV k="labeled_missed_24h" v={status.pipeline.labeled_missed_24h} />
            <KV k="candidate_share_24h" v={status.pipeline.candidate_share_24h ?? "—"} />
            <KV k="delivery.terminal_24h" v={status.delivery.terminal_24h} />
            <KV k="delivery.last_error_code" v={status.delivery.last_error_code ?? "—"} />
          </dl>
        </section>
        <section>
          <h4>接入</h4>
          <dl className="news-kv">
            <KV k="connected" v={String(status.ingest.connected)} />
            <KV k="token_configured" v={String(status.ingest.token_configured)} />
            <KV k="last_frame_at_ms" v={optionalTime(status.ingest.last_frame_at_ms)} />
            <KV k="last_publish_at_ms" v={optionalTime(status.ingest.last_publish_at_ms)} />
            <KV k="last_error_code" v={status.ingest.last_error_code ?? "—"} />
            <KV
              k="provider_enabled_strategy_count"
              v={status.ingest.provider_enabled_strategy_ids?.length ?? "—"}
            />
            <KV
              k="configured_strategy_count"
              v={(status.ingest.configured_strategy_ids ?? []).length}
            />
            <KV
              k="open_incidents"
              v={
                incidents.length
                  ? incidents
                      .map(
                        (incident) =>
                          `${incident.cause_class} @ ${absoluteTime(incident.opened_at_ms)}`,
                      )
                      .join("; ")
                  : "—"
              }
            />
          </dl>
        </section>
        <section>
          <h4>队列</h4>
          <dl className="news-kv">
            <KV k="configured" v={String(status.broker.configured)} />
            <KV k="connected" v={String(status.broker.connected ?? "unknown")} />
            <KV k="observed_at_ms" v={optionalTime(status.broker.observed_at_ms)} />
            <KV k="error_code" v={status.broker.error_code ?? "—"} />
            {queues.map(([name, queue]) => (
              <KV k={name} key={name} v={`${queue.messages} 条 · ${queue.consumers} 消费者`} />
            ))}
          </dl>
        </section>
      </div>
    </details>
  );
}

function KV({ k, v }: { k: string; v: string | number }) {
  return (
    <div className="news-kv-row">
      <dt>{k}</dt>
      <dd>{String(v)}</dd>
    </div>
  );
}
