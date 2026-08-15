import * as PageState from "@shared/ui/PageState";
import { ExternalLink } from "lucide-react";
import type { ReactNode } from "react";

import "./newsOperations.css";
import { NewsSectionTabs } from "./NewsSectionTabs";
import { formatNewsLocalTimestamp } from "./newsTime";
import {
  type NewsSource,
  type NewsStatus,
  useNewsSourcesWithToken,
  useNewsStatusWithToken,
} from "./useNewsPage";

export function NewsStatusRoute({ token }: { token: string }) {
  const query = useNewsStatusWithToken(token);
  const status = query.data;

  return (
    <section
      aria-label="新闻运行状态"
      className="radar-panel news-panel news-operations-shell"
      data-page-archetype="scan"
    >
      <NewsSectionTabs active="status" />
      <header className="news-operations-header">
        <div>
          <span className="news-eyebrow">STRATEGY NEWS STATUS</span>
          <h1>新闻运行状态</h1>
          <p>账户 Strategy 自动推送、新闻事件、简报与推送链路的当前状态。</p>
        </div>
        {status ? (
          <span className="news-operations-state" data-state={status.operating_state}>
            {operatingStateLabel(status.operating_state)}
          </span>
        ) : null}
      </header>

      {query.isLoading && !status ? (
        <PageState.Loading layout="panel" rows={4} label="正在读取新闻运行状态" />
      ) : null}
      {query.isError && !status ? (
        <PageState.Error error={query.error} onRetry={() => void query.refetch()} />
      ) : null}
      {status ? (
        <PageState.Stale updating={query.isFetching && !query.isLoading}>
          <StatusDocument status={status} />
        </PageState.Stale>
      ) : null}
    </section>
  );
}

function StatusDocument({ status }: { status: NewsStatus }) {
  const { brief, ingest, push, story } = status.layers;
  const titlePresentation = status.title_presentation;
  const opennews = ingest.opennews;
  return (
    <div className="news-status-document">
      <header className="news-status-overview">
        <dl>
          <StatusFact label="整体状态" value={statusLabel(status.status)} />
          <StatusFact label="测量时间" value={absoluteTime(status.measured_at_ms)} />
          <StatusFact
            label="最近成功"
            value={
              status.last_success_at_ms == null ? "尚无" : absoluteTime(status.last_success_at_ms)
            }
          />
        </dl>
        <StatusReasons reasons={status.reasons} />
      </header>

      <div className="news-status-layer-grid">
        <StatusLayer title="公开采集" status={ingest.status} reasons={ingest.reasons}>
          {opennews ? (
            <>
              <StatusFact label="WSS 当前状态" value={wssStateLabel(status.realtime.wss_state)} />
              <StatusFact
                label="WSS 连接时间"
                value={optionalTime(status.realtime.connected_at_ms)}
              />
              <StatusFact
                label="入站延迟 P95"
                value={optionalDuration(status.realtime.inbound_latency.p95_ms)}
              />
              <StatusFact
                label="Story 可见 P95"
                value={optionalDuration(status.realtime.story_visible_latency.p95_ms)}
              />
              <StatusFact
                label="配置 Strategy"
                value={String(opennews.configured_strategy_count)}
              />
              <StatusFact
                label="已观察 Strategy"
                value={String(opennews.observed_strategy_count)}
              />
              <StatusFact
                label="最近 Strategy 触发"
                value={optionalTime(opennews.last_accepted_strategy_trigger_at_ms)}
              />
              <StatusFact
                label="历史校验"
                value={historyStatusLabel(opennews.strategy_history_status)}
              />
              <StatusFact
                label="最近历史校验"
                value={optionalTime(opennews.last_history_check_at_ms)}
              />
              <StatusFact label="未结事故" value={String(opennews.unresolved_incident_count)} />
              <StatusFact
                label="最近批次接收"
                value={`${opennews.last_items_accepted}/${opennews.last_items_seen}`}
              />
              <StatusFact label="连续失败" value={String(opennews.consecutive_failures)} />
            </>
          ) : null}
          <StatusFact label="RSS 印证服务" value={ingest.rss.enabled ? "已启用" : "未启用"} />
          {ingest.rss.enabled ? (
            <>
              <StatusFact
                label="RSS 印证成功来源"
                value={`${ingest.rss.successful_source_count}/${ingest.rss.source_count}`}
              />
              <StatusFact label="RSS 印证失败来源" value={String(ingest.rss.failed_source_count)} />
              <StatusFact label="RSS 印证采集中" value={String(ingest.rss.claimed_source_count)} />
              <StatusFact
                label="RSS 印证最近成功"
                value={optionalTime(ingest.rss.latest_success_at_ms)}
              />
            </>
          ) : null}
        </StatusLayer>

        <StatusLayer title="新闻事件" status={story.status} reasons={story.reasons}>
          <StatusFact label="当前事件" value={String(story.active_stories)} />
          <StatusFact label="当前报道" value={String(story.active_items)} />
          <StatusFact label="最近成功" value={optionalTime(story.last_success_at_ms)} />
          <StatusFact label="最新事件" value={optionalTime(story.newest_story_at_ms)} />
          <StatusFact label="一致性错误" value={String(story.invariant_error_count)} />
          <StatusFact label="无效归属" value={String(story.invalid_owner_count)} />
        </StatusLayer>

        <StatusLayer title="公共简报" status={brief.status} reasons={brief.reasons}>
          <StatusFact label="公开状态" value={briefStateLabel(brief.public_state)} />
          <StatusFact label="当前时段" value={optionalTime(brief.slot_at_ms)} />
          <StatusFact label="下次评估" value={absoluteTime(brief.next_due_at_ms)} />
          <StatusFact
            label="最近任务"
            value={brief.latest_run ? runStateLabel(brief.latest_run.status) : "尚无"}
          />
        </StatusLayer>

        <StatusLayer
          title="标题呈现（信息）"
          status={titlePresentation.status}
          reasons={titlePresentation.reasons}
        >
          <StatusFact
            label="DeepL"
            value={
              titlePresentation.deepl_configured
                ? `已配置 ${titlePresentation.deepl_key_count} Key`
                : "未配置"
            }
          />
          <StatusFact
            label="DeepSeek 降级"
            value={titlePresentation.deepseek_configured ? "已配置" : "未配置"}
          />
          <StatusFact label="策略版本" value={titlePresentation.policy_version} />
          <StatusFact label="待处理" value={String(titlePresentation.pending_count)} />
          <StatusFact label="处理中" value={String(titlePresentation.resolving_count)} />
          <StatusFact
            label="最早阻塞 Push"
            value={optionalTime(titlePresentation.oldest_push_blocking_at_ms)}
          />
          <StatusFact
            label="DeepL / DeepSeek 时限"
            value={`${optionalDuration(titlePresentation.deepl_deadline_ms)} / ${optionalDuration(titlePresentation.deepseek_deadline_ms)}`}
          />
          <StatusFact
            label="24 小时呈现"
            value={
              titlePresentation.resolution_24h.sample_complete
                ? `译文 ${titlePresentation.resolution_24h.translated} · 无需 ${titlePresentation.resolution_24h.not_needed} · 原文 ${titlePresentation.resolution_24h.fallback}`
                : "超过采样上限"
            }
          />
          <StatusFact
            label="24 小时时延 P95"
            value={optionalDuration(titlePresentation.resolution_24h.latency_p95_ms)}
          />
        </StatusLayer>

        <StatusLayer title="新闻推送" status={push.status} reasons={push.reasons}>
          <StatusFact label="配置意图" value={push.requested ? "已请求" : "未请求"} />
          <StatusFact label="飞书投递" value={push.delivery_available ? "可用" : "不可用"} />
          <StatusFact label="待发送" value={String(push.pending_count)} />
          <StatusFact label="发送中" value={String(push.sending_count)} />
          <StatusFact label="已发送" value={String(push.sent_count)} />
          <StatusFact label="已终结" value={String(push.terminal_count)} />
          <StatusFact label="重复抑制（信息）" value={String(push.suppressed_count)} />
          <StatusFact label="Admission 策略" value={push.admission_policy_version} />
          <StatusFact label="Exact Atom 身份" value={push.comparison_identity_version} />
          <StatusFact
            label="24 小时发送时延 P95"
            value={
              push.delivery_24h.sample_complete
                ? optionalDuration(push.delivery_24h.latency_p95_ms)
                : "超过采样上限"
            }
          />
        </StatusLayer>
      </div>
      {opennews?.incidents.length ? <IncidentLedger incidents={opennews.incidents} /> : null}
    </div>
  );
}

function IncidentLedger({
  incidents,
}: {
  incidents: NonNullable<NewsStatus["layers"]["ingest"]["opennews"]>["incidents"];
}) {
  return (
    <details className="news-incident-ledger">
      <summary>历史 WSS 事故 · {incidents.length}</summary>
      <ol>
        {incidents.map((incident) => (
          <li key={incident.incident_id}>
            <b>{incidentCauseLabel(incident.cause_class)}</b>
            <span>{absoluteTime(incident.opened_at_ms)}</span>
            <span>{incidentRecoveryLabel(incident.recovery_status)}</span>
          </li>
        ))}
      </ol>
    </details>
  );
}

function StatusLayer({
  children,
  reasons,
  status,
  title,
}: {
  children: ReactNode;
  reasons: string[];
  status: "degraded" | "disabled" | "ready" | "warming";
  title: string;
}) {
  return (
    <article className="news-status-layer" data-state={status}>
      <header>
        <h2>{title}</h2>
        <span>{statusLabel(status)}</span>
      </header>
      <dl>{children}</dl>
      <StatusReasons reasons={reasons} />
    </article>
  );
}

function StatusFact({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}

function StatusReasons({ reasons }: { reasons: string[] }) {
  if (!reasons.length) return null;
  return (
    <ul aria-label="状态原因" className="news-status-reasons">
      {reasons.map((reason, index) => (
        <li key={`${reason}:${index}`}>{reason}</li>
      ))}
    </ul>
  );
}

export function NewsSourcesRoute({ token }: { token: string }) {
  const query = useNewsSourcesWithToken(token);
  const pages = query.data?.pages ?? [];
  const sources = pages.flatMap((page) => page.items);

  return (
    <section
      aria-label="新闻来源"
      className="radar-panel news-panel news-operations-shell"
      data-page-archetype="scan"
    >
      <NewsSectionTabs active="sources" />
      <header className="news-operations-header">
        <div>
          <span className="news-eyebrow">NEWS SOURCES</span>
          <h1>公开新闻来源</h1>
          <p>OpenNews 账户 Strategy 自动推送；公共 RSS 覆盖与交叉印证链可由配置启用。</p>
        </div>
        {sources.length ? <span className="news-source-count">已加载 {sources.length}</span> : null}
      </header>

      {query.isLoading && !query.data ? (
        <PageState.Loading layout="panel" rows={6} label="正在读取新闻来源" />
      ) : null}
      {query.isError && !query.data ? (
        <PageState.Error error={query.error} onRetry={() => void query.refetch()} />
      ) : null}
      {!query.isLoading && !query.isError && !sources.length ? (
        <PageState.Empty hint="等待服务端来源目录。" title="暂无新闻来源" />
      ) : null}
      {sources.length ? (
        <PageState.Stale updating={query.isFetching && !query.isFetchingNextPage}>
          <div className="news-source-list">
            {sources.map((source) => (
              <SourceCard key={source.source_id} source={source} />
            ))}
          </div>
          {query.hasNextPage ? (
            <button
              className="news-sources-load-more"
              disabled={query.isFetchingNextPage}
              onClick={() => void query.fetchNextPage()}
              type="button"
            >
              {query.isFetchingNextPage ? "正在加载" : "加载更多来源"}
            </button>
          ) : null}
        </PageState.Stale>
      ) : null}
    </section>
  );
}

function SourceCard({ source }: { source: NewsSource }) {
  const rejectionEntries = Object.entries(source.last_rejection_counts);
  const sourceState = getSourceState(source);
  return (
    <article className="news-source-card" data-state={sourceState.state}>
      <header>
        <div>
          <span>
            {source.source_kind === "rss"
              ? `RSS · TIER ${source.tier}`
              : "OPENNEWS · STRATEGY 自动推送"}
          </span>
          <h2>{source.name}</h2>
          <code>{source.source_id}</code>
        </div>
        <span>{sourceState.label}</span>
      </header>
      <dl>
        <StatusFact label="最近成功" value={optionalTime(source.last_success_at_ms)} />
        <StatusFact label="最近结果" value={source.last_outcome ?? "尚未执行"} />
        <StatusFact
          label="接收报道"
          value={`${source.last_items_accepted}/${source.last_items_seen}`}
        />
        <StatusFact label="连续失败" value={String(source.consecutive_failures)} />
        {source.source_kind === "opennews" ? (
          <>
            <StatusFact
              label="最近 Strategy 触发"
              value={optionalTime(source.last_accepted_strategy_trigger_at_ms)}
            />
            <StatusFact
              label="Strategy 历史校验"
              value={historyStatusLabel(source.strategy_history_status)}
            />
            <StatusFact label="最近校验" value={optionalTime(source.last_history_check_at_ms)} />
          </>
        ) : null}
      </dl>
      {source.feed_url && isHttpUrl(source.feed_url) ? (
        <a href={source.feed_url} rel="noreferrer" target="_blank">
          打开公开 Feed
          <ExternalLink aria-hidden />
        </a>
      ) : null}
      {source.last_error ? <p className="news-source-error">{source.last_error}</p> : null}
      {source.incidents.length ? <IncidentLedger incidents={source.incidents} /> : null}
      {rejectionEntries.length ? (
        <details className="news-source-rejections">
          <summary>最近剔除原因</summary>
          <dl>
            {rejectionEntries.map(([reason, count]) => (
              <StatusFact key={reason} label={reason} value={String(count)} />
            ))}
          </dl>
        </details>
      ) : null}
    </article>
  );
}

function getSourceState(source: NewsSource): { label: string; state: string } {
  if (!source.enabled) return { label: "已停用", state: "disabled" };
  if (source.last_error || source.consecutive_failures > 0) {
    return { label: "采集异常", state: "degraded" };
  }
  if (source.last_success_at_ms == null) return { label: "等待首次采集", state: "warming" };
  if (source.source_kind === "opennews" && !source.live_connected) {
    return { label: "Strategy 连接异常", state: "degraded" };
  }
  return { label: "采集正常", state: "ready" };
}

function wssStateLabel(state: NewsStatus["realtime"]["wss_state"]): string {
  if (state === "connected") return "已连接";
  if (state === "reconnecting") return "重连中";
  return "不可用";
}

function historyStatusLabel(
  status: NonNullable<NewsStatus["layers"]["ingest"]["opennews"]>["strategy_history_status"],
): string {
  if (status === "available") return "可用";
  if (status === "partial") return "部分恢复";
  if (status === "unavailable") return "官方接口不可用";
  return "待校验";
}

function incidentCauseLabel(
  cause: NonNullable<
    NewsStatus["layers"]["ingest"]["opennews"]
  >["incidents"][number]["cause_class"],
): string {
  const labels: Record<typeof cause, string> = {
    authentication: "认证失败",
    buffer_overflow: "本地缓冲溢出",
    database_backpressure: "数据库背压",
    network_connect: "网络连接",
    idle_timeout: "空闲超时",
    legacy_unknown: "历史原因未知",
    planned_shutdown: "计划内停机",
    process_outage: "进程中断",
    protocol_error: "协议错误",
    provider_close: "上游关闭",
    unknown: "未知",
  };
  return labels[cause];
}

function incidentRecoveryLabel(
  state: NonNullable<
    NewsStatus["layers"]["ingest"]["opennews"]
  >["incidents"][number]["recovery_status"],
): string {
  const labels: Record<typeof state, string> = {
    not_required: "无需恢复",
    partial: "部分恢复",
    pending: "待恢复",
    recovered: "已恢复",
    running: "恢复中",
    unavailable: "官方历史不可用",
  };
  return labels[state];
}

function operatingStateLabel(state: NewsStatus["operating_state"]): string {
  if (state === "live") return "运行正常";
  if (state === "recovering") return "准备中";
  return "运行受阻";
}

function statusLabel(status: "degraded" | "disabled" | "ready" | "warming"): string {
  if (status === "ready") return "正常";
  if (status === "warming") return "准备中";
  if (status === "disabled") return "未启用";
  return "异常";
}

function briefStateLabel(state: NewsStatus["layers"]["brief"]["public_state"]): string {
  if (state === "current") return "当前";
  if (state === "degraded") return "当前 · 增强降级";
  if (state === "last_known_good") return "上一份完整简报";
  return "不可用";
}

function runStateLabel(status: "completed" | "due" | "running"): string {
  if (status === "completed") return "已完成";
  if (status === "running") return "执行中";
  return "待执行";
}

function optionalTime(value: number | null): string {
  return value == null ? "尚无" : absoluteTime(value);
}

function absoluteTime(value: number): string {
  return formatNewsLocalTimestamp(value);
}

function optionalDuration(value: number | null): string {
  if (value == null) return "尚无";
  return value < 1_000 ? `${value} 毫秒` : `${(value / 1_000).toFixed(1)} 秒`;
}

function isHttpUrl(value: string): boolean {
  return /^https?:\/\//i.test(value.trim());
}
