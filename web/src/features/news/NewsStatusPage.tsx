import * as PageState from "@shared/ui/PageState";
import type { ReactNode } from "react";

import "./newsStatus.css";
import { NewsSectionTabs } from "./NewsSectionTabs";
import { absoluteTime, optionalDuration, optionalTime } from "./newsLabels";
import { type NewsIncident, type NewsStatus, useNewsStatusWithToken } from "./useNewsPage";

export function NewsStatusPage({ token }: { token: string }) {
  const query = useNewsStatusWithToken(token);
  const status = query.data;

  return (
    <section
      aria-label="新闻运行状态"
      className="news-panel news-status-shell"
      data-page-archetype="scan"
    >
      <NewsSectionTabs active="status" />
      <header className="news-status-header">
        <div>
          <span className="news-eyebrow">NEWS PIPELINE STATUS</span>
          <h1>新闻运行状态</h1>
          <p>OpenNews 接入、消息代理、Triage/Analyst 流水线与飞书推送的当前状态。</p>
        </div>
        {status ? (
          <span className="news-status-state" data-state={status.state}>
            {overallStateLabel(status.state)}
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
  const { broker, control, delivery, ingest, pipeline } = status;
  const openIncidents = ingest.open_incidents ?? [];
  const strategyWarnings = ingest.strategy_warnings ?? [];
  const queues = Object.entries(broker.queues ?? {});
  const mutes = control.mutes ?? [];
  const watchSymbols = status.watchlist ?? [];
  return (
    <div className="news-status-document">
      <header className="news-status-overview">
        <dl>
          <StatusFact label="整体状态" value={overallStateLabel(status.state)} />
          <StatusFact label="Workers" value={status.workers_state ?? "未知"} />
          <StatusFact label="测量时间" value={absoluteTime(status.measured_at_ms)} />
        </dl>
      </header>

      <div className="news-status-layer-grid">
        <StatusLayer
          footer={
            <>
              <StatusReasons reasons={strategyWarnings} />
              {openIncidents.length ? <IncidentLedger incidents={openIncidents} /> : null}
            </>
          }
          title="接入 · OpenNews WSS"
        >
          <StatusFact label="Token" value={ingest.token_configured ? "已配置" : "未配置"} />
          <StatusFact label="WSS" value={ingest.connected ? "已连接" : "未连接"} />
          <StatusFact label="最近帧" value={optionalTime(ingest.last_frame_at_ms)} />
          <StatusFact label="最近发布" value={optionalTime(ingest.last_publish_at_ms)} />
          <StatusFact label="最近错误" value={ingest.last_error_code ?? "无"} />
          <StatusFact
            label="配置 Strategy"
            value={String((ingest.configured_strategy_ids ?? []).length)}
          />
          <StatusFact
            label="提供方启用 Strategy"
            value={
              ingest.provider_enabled_strategy_ids == null
                ? "未校验"
                : String(ingest.provider_enabled_strategy_ids.length)
            }
          />
          <StatusFact label="未结事故" value={String(openIncidents.length)} />
        </StatusLayer>

        <StatusLayer title="代理 · RabbitMQ">
          <StatusFact label="配置" value={broker.configured ? "已配置" : "未配置"} />
          <StatusFact
            label="连接"
            value={broker.connected == null ? "未知" : broker.connected ? "已连接" : "未连接"}
          />
          <StatusFact label="错误" value={broker.error_code ?? "无"} />
          {queues.map(([name, queue]) => (
            <StatusFact
              key={name}
              label={`队列 ${name}`}
              value={`${queue.messages} 消息 · ${queue.consumers} 消费者`}
            />
          ))}
        </StatusLayer>

        <StatusLayer title="流水线 · Triage / Analyst">
          <StatusFact label="1h 事件" value={String(pipeline.events_1h)} />
          <StatusFact label="24h 事件" value={String(pipeline.events_24h)} />
          <StatusFact label="24h 候选" value={String(pipeline.candidates_24h)} />
          <StatusFact label="24h Triage" value={String(pipeline.triage_24h)} />
          <StatusFact label="24h Triage 降级" value={String(pipeline.triage_degraded_24h)} />
          <StatusFact label="24h Analyst" value={String(pipeline.deep_24h)} />
          <StatusFact label="24h 判定推送" value={String(pipeline.decided_push_24h)} />
          <StatusFact label="24h 节流" value={String(pipeline.throttled_24h)} />
          <StatusFact label="Triage P50" value={optionalDuration(pipeline.triage_p50_ms)} />
          <StatusFact label="Triage P95" value={optionalDuration(pipeline.triage_p95_ms)} />
          <StatusFact label="Triage 模型" value={pipeline.triage_model ?? "未配置"} />
          <StatusFact label="Analyst 模型" value={pipeline.analyst_model ?? "未配置"} />
        </StatusLayer>

        <StatusLayer title="推送 · 飞书">
          <StatusFact label="飞书投递" value={delivery.delivery_available ? "可用" : "不可用"} />
          <StatusFact label="1h 已发送" value={String(delivery.sent_1h)} />
          <StatusFact label="24h 已发送" value={String(delivery.sent_24h)} />
          <StatusFact label="24h 已终结" value={String(delivery.terminal_24h)} />
          <StatusFact label="小时上限" value={String(delivery.hourly_cap)} />
          <StatusFact label="端到端 P95" value={optionalDuration(delivery.e2e_p95_ms)} />
          <StatusFact label="最近错误" value={delivery.last_error_code ?? "无"} />
        </StatusLayer>
      </div>

      <div className="news-status-control-grid">
        <article className="news-status-control" data-state={control.paused ? "paused" : "running"}>
          <header>
            <h2>控制（只读）</h2>
            <span>{control.paused ? "已暂停" : "运行中"}</span>
          </header>
          {mutes.length ? (
            <ul className="news-status-mutes">
              {mutes.map((mute, index) => (
                <li key={`${index}:${compactJson(mute)}`}>{compactJson(mute)}</li>
              ))}
            </ul>
          ) : (
            <p>无静音规则。</p>
          )}
        </article>
        <article className="news-watch-list">
          <header>
            <h2>关注名单（只读）</h2>
            <span>{watchSymbols.length} 个资产</span>
          </header>
          {watchSymbols.length ? (
            <ul>
              {watchSymbols.map((symbol) => (
                <li key={symbol}>{symbol}</li>
              ))}
            </ul>
          ) : (
            <p>未配置关注资产。</p>
          )}
        </article>
      </div>
    </div>
  );
}

function IncidentLedger({ incidents }: { incidents: readonly NewsIncident[] }) {
  return (
    <details className="news-incident-ledger">
      <summary>未结 WSS 事故 · {incidents.length}</summary>
      <ol>
        {incidents.map((incident) => (
          <li key={incident.incident_id}>
            <b>{incident.cause_class}</b>
            <span>{absoluteTime(incident.opened_at_ms)}</span>
            <span>{incident.planned ? "计划内" : "非计划"}</span>
          </li>
        ))}
      </ol>
    </details>
  );
}

function StatusLayer({
  children,
  footer,
  title,
}: {
  children: ReactNode;
  footer?: ReactNode;
  title: string;
}) {
  return (
    <article className="news-status-layer">
      <header>
        <h2>{title}</h2>
      </header>
      <dl>{children}</dl>
      {footer}
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

function StatusReasons({ reasons }: { reasons: readonly string[] }) {
  if (!reasons.length) return null;
  return (
    <ul aria-label="状态原因" className="news-status-reasons">
      {reasons.map((reason, index) => (
        <li key={`${reason}:${index}`}>{reason}</li>
      ))}
    </ul>
  );
}

function overallStateLabel(state: NewsStatus["state"]): string {
  if (state === "ready") return "运行正常";
  if (state === "warming") return "准备中";
  if (state === "degraded") return "运行降级";
  return "不可用";
}

function compactJson(value: unknown): string {
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}
