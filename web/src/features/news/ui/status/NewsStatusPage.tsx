import { newsPath } from "@shared/routing/paths";
import { Bar } from "@shared/ui/Bar";
import { Card } from "@shared/ui/Card";
import { KeyValue, KeyValueRow } from "@shared/ui/KeyValue";
import * as PageState from "@shared/ui/PageState";
import { TriangleAlert } from "lucide-react";
import { Link } from "react-router-dom";

import {
  type NewsHealthItem,
  type NewsReasonCount,
  type NewsStatus,
  useNewsStatusWithToken,
} from "../../api/newsQueries";
import {
  HEALTH_ITEM_KEYS,
  absoluteTime,
  formatCount,
  healthItemEyebrow,
  healthItemTitle,
  healthLevelLabel,
  healthTone,
  optionalDuration,
  optionalTime,
  percent,
  reasonStageLabel,
  reasonStageTone,
} from "../../model/newsLabels";
import { NewsEmptyNote, NewsPageHeader, NewsPageShell, NewsTechnical } from "../chrome/NewsChrome";
import { NewsOverallPill } from "../chrome/NewsHealthPill";
import { NewsToneDot } from "../chrome/NewsTone";

import { InstrumentUniverse } from "./NewsInstrumentUniverse";

import "./newsStatus.css";

/**
 * Which reason groups the page shows, in reading order. This list is the render filter, so a stage the
 * server sends that is missing here is silently dropped — `ungrounded` shipped computed, labelled and
 * documented but invisible for exactly that reason (#87 review).
 */
const REASON_STAGE_ORDER = ["push", "throttle", "drop", "ungrounded", "gate", "degraded"] as const;

export function NewsStatusPage({ token }: { token: string }) {
  const query = useNewsStatusWithToken(token);
  const status = query.data;
  return (
    <NewsPageShell archetype="scan" className="news-status-shell" label="新闻流水线状态">
      <NewsPageHeader
        subtitle="四个环节的健康度、过去 24 小时的去向，以及当前控制状态。"
        title="流水线状态"
      >
        {status?.health ? <NewsOverallPill status={status} /> : null}
      </NewsPageHeader>

      {query.isLoading && !status ? (
        <PageState.Loading label="正在读取流水线状态" layout="panel" rows={4} />
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
                <HealthCard item={status.health[key]} key={key} name={key}>
                  <HealthNumbers keyName={key} status={status} />
                </HealthCard>
              ))}
            </div>

            <Card
              aria-label="标的表快照"
              flush
              hint="Binance / Hyperliquid 公开目录，用于符号归一与落表判断"
              title="标的表快照"
            >
              <InstrumentUniverse status={status} />
            </Card>

            <div className="news-status-grid">
              <Card
                aria-label="过去 24 小时漏斗"
                hint="点一层跳到对应事件"
                title="过去 24 小时去向"
              >
                <Funnel status={status} />
              </Card>
              <Card aria-label="拦截与推送原因" hint="过去 24 小时，按事件数" title="原因排行">
                <ReasonBars reasons={status.reasons_24h ?? []} />
              </Card>
            </div>

            <div className="news-status-grid">
              <Card aria-label="控制状态" hint="用 tracefold news control 修改" title="控制">
                <ControlView status={status} />
              </Card>
              <Card aria-label="关注列表与策略" title="关注列表与策略">
                <WatchAndStrategies status={status} />
              </Card>
            </div>

            <TechnicalMetrics status={status} />
          </div>
        </PageState.Stale>
      ) : null}
    </NewsPageShell>
  );
}

/**
 * One environment, one thresholded read. The eyebrow names the stage in the pipeline's own Latin vocabulary
 * so a card lines up with the thing an operator would grep; the title is the server's Chinese sentence, and
 * the 2px top edge is the level. The browser never computes a second health state — level, summary and
 * detail all arrive decided.
 */
function HealthCard({
  children,
  item,
  name,
}: {
  children?: React.ReactNode;
  item: NewsHealthItem;
  name: (typeof HEALTH_ITEM_KEYS)[number];
}) {
  const tone = healthTone(item.level);
  return (
    <article className="news-health-card news-toned" data-level={item.level} data-tone={tone}>
      <header>
        <span className="news-health-card-eyebrow">{healthItemEyebrow(name)}</span>
        <span className="news-health-card-level">{healthLevelLabel(item.level)}</span>
      </header>
      <p className="news-health-card-summary">
        <span className="sr-only">{healthItemTitle(name)}：</span>
        {item.summary_zh}
      </p>
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
          ["FRAME", optionalTime(status.ingest.last_frame_at_ms).slice(11) || "尚无"],
          ["EVT 1H", formatCount(status.pipeline.events_1h)],
        ]
      : keyName === "broker"
        ? [
            ["TRIAGE", formatCount(status.broker.queues?.["news.triage"]?.messages ?? 0)],
            ["DELIVER", formatCount(status.broker.queues?.["news.deliver"]?.messages ?? 0)],
          ]
        : keyName === "model"
          ? [
              ["JUDGED", formatCount(status.pipeline.triage_24h)],
              ["P95", optionalDuration(status.pipeline.triage_p95_ms)],
            ]
          : [["SENT", formatCount(status.delivery.sent_24h)]];
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
      hint: `1h ${formatCount(funnel.received_1h)}`,
      label: "收到",
      to: newsPath(),
      value: funnel.received,
    },
    {
      hint: percent(funnel.candidates, funnel.received),
      label: "送审",
      to: null,
      value: funnel.candidates,
    },
    {
      hint: percent(funnel.triaged, funnel.received),
      label: "模型判断",
      to: null,
      value: funnel.triaged,
    },
    {
      hint: percent(funnel.decided_push, funnel.received),
      label: "决定推送",
      to: `${newsPath()}?outcome=pushed`,
      value: funnel.decided_push,
    },
    {
      hint: `1h ${formatCount(funnel.delivered_1h)}`,
      label: "已送达",
      to: `${newsPath()}?outcome=pushed`,
      value: funnel.delivered,
    },
  ];
  return (
    <>
      {/*
       * The bars are the chain and only the chain: 收到 ⊇ 送审 ⊇ 模型判断 ⊇ 决定推送 ⊇ 已送达, so every band is a
       * share of the one above it and the drop sentence below can name any two adjacent rows. 符号落表 is
       * deliberately not here — an Event whose symbols never landed can still have been pushed, so it is a
       * property of the Events in these bands rather than a band of its own, and it gets a tile on the feed
       * where its own denominator (the Events that carried a tag) fits beside it.
       */}
      <ol className="news-funnel">
        {layers.map((layer) => {
          const content = (
            <>
              <span className="news-funnel-label">{layer.label}</span>
              <Bar
                share={Math.max(4, (layer.value / max) * 100)}
                size="lg"
                tone={layer.to ? "accent" : "neutral"}
              />
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
      <BiggestDrop status={status} />
    </>
  );
}

/**
 * Which step loses the most. It is a subtraction between two adjacent server numbers — the console points at
 * the layer, the reader goes and reads the reasons beside it.
 */
function BiggestDrop({ status }: { status: NewsStatus }) {
  const funnel = status.funnel_24h;
  const drops = [
    { from: "收到", lost: funnel.received - funnel.candidates, to: "送审" },
    { from: "送审", lost: funnel.candidates - funnel.triaged, to: "模型判断" },
    { from: "模型判断", lost: funnel.triaged - funnel.decided_push, to: "决定推送" },
    { from: "决定推送", lost: funnel.decided_push - funnel.delivered, to: "已送达" },
  ];
  const worst = drops.reduce((best, drop) => (drop.lost > best.lost ? drop : best), drops[0]);
  if (worst.lost <= 0) return null;
  return (
    <p className="news-funnel-note">
      最大流失在「{worst.from} → {worst.to}」，{formatCount(worst.lost)} 条没有往下走。
    </p>
  );
}

function ReasonBars({ reasons }: { reasons: NewsReasonCount[] }) {
  if (!reasons.length) return <NewsEmptyNote>过去 24 小时没有记录。</NewsEmptyNote>;
  const max = Math.max(1, ...reasons.map((reason) => reason.count));
  const groups = REASON_STAGE_ORDER.map((stage) => ({
    rows: reasons.filter((reason) => reason.stage === stage).slice(0, 6),
    stage,
  })).filter((group) => group.rows.length);
  return (
    <div className="news-reason-groups">
      {groups.map((group) => (
        <section
          className="news-reason-group news-toned"
          data-stage={group.stage}
          data-tone={reasonStageTone(group.stage)}
          key={group.stage}
        >
          <h3>
            <NewsToneDot />
            {reasonStageLabel(group.stage)}
            <span className="news-reason-total">
              {formatCount(group.rows.reduce((sum, reason) => sum + reason.count, 0))}
            </span>
          </h3>
          <ul>
            {group.rows.map((reason) => (
              <li key={`${reason.stage}-${reason.key}`} title={reason.key}>
                <span className="news-reason-label">{reason.label_zh}</span>
                <span className="news-reason-bar">
                  <Bar
                    share={Math.max(3, (reason.count / max) * 100)}
                    size="sm"
                    tone={reasonBarTone(group.stage)}
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

/** Only the two groups worth pointing at take a colour; the rest are inventory and stay neutral grey. */
function reasonBarTone(stage: string) {
  if (stage === "push") return "accent" as const;
  if (stage === "ungrounded" || stage === "throttle") return "caution" as const;
  return "neutral" as const;
}

/**
 * Read-only on purpose. Pause and mute are written by `tracefold news control` and read on every message; a
 * browser button would be a second writer for state the pipeline consults on the hot path.
 */
function ControlView({ status }: { status: NewsStatus }) {
  const mutes = status.control.mutes as Array<Record<string, unknown>>;
  const paused = Boolean(status.control.paused);
  return (
    <div className="news-control">
      <p className="news-control-state news-toned" data-tone={paused ? "caution" : "done"}>
        <NewsToneDot />
        <b>{paused ? "推送已暂停" : "推送运行中"}</b>
      </p>
      <h3 className="news-control-heading">生效中的静音 {formatCount(mutes.length)}</h3>
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
                <td>
                  <code>{String(mute.key ?? mute.symbol ?? mute.storyline_key ?? "")}</code>
                </td>
                <td>{typeof mute.until_ms === "number" ? absoluteTime(mute.until_ms) : "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <NewsEmptyNote>没有生效中的静音。</NewsEmptyNote>
      )}
    </div>
  );
}

function WatchAndStrategies({ status }: { status: NewsStatus }) {
  const warnings = status.ingest.strategy_warnings ?? [];
  const watchlist = status.watchlist ?? [];
  const configuredIds = status.ingest.configured_strategy_ids ?? [];
  const providerIds = status.ingest.provider_enabled_strategy_ids;
  const configured = configuredIds.length;
  /*
   * Counts only: the Strategy IDs are private account configuration and never reach the browser as rendered
   * values. The matched figure has to be a real intersection — comparing the two lengths would report a full
   * match for three configured IDs the provider has never heard of, contradicting `strategy_warnings` right
   * below it. When the provider list is unavailable there is nothing to match against, so no ratio is shown.
   */
  const matched = providerIds
    ? configuredIds.filter((id) => providerIds.includes(id)).length
    : null;
  return (
    <div className="news-watch">
      <h3 className="news-control-heading">关注列表 {formatCount(watchlist.length)}</h3>
      <div className="news-chip-row">
        {watchlist.length ? (
          watchlist.map((symbol) => <code key={symbol}>{symbol}</code>)
        ) : (
          <em>未配置</em>
        )}
      </div>
      <h3 className="news-control-heading">Strategy</h3>
      <p className="news-strategy-count">
        {formatCount(matched ?? configured)}
        <small> / {formatCount(configured)} 已配置</small>
      </p>
      {matched == null ? (
        <p className="news-strategy-note">provider 列表暂不可用，无法核对</p>
      ) : (
        <Bar share={configured ? (matched / configured) * 100 : 0} tone="accent" />
      )}
      {warnings.length ? (
        <div className="news-warning-block news-toned" data-tone="caution">
          <TriangleAlert aria-hidden />
          <ul>
            {warnings.map((warning) => (
              <li key={warning}>{warning}</li>
            ))}
          </ul>
        </div>
      ) : null}
    </div>
  );
}

function TechnicalMetrics({ status }: { status: NewsStatus }) {
  const queues = Object.entries(status.broker.queues ?? {});
  const incidents = status.ingest.open_incidents ?? [];
  return (
    <NewsTechnical summary="技术指标（延迟分位、队列深度、事故、原始计数）">
      <section>
        <h4>流水线</h4>
        <KeyValue>
          <KeyValueRow k="state" v={status.state} />
          <KeyValueRow k="workers_state" v={status.workers_state ?? "—"} />
          <KeyValueRow k="triage_model" v={status.pipeline.triage_model ?? "—"} />
          <KeyValueRow k="triage_p50_ms" v={optionalDuration(status.pipeline.triage_p50_ms)} />
          <KeyValueRow k="triage_p95_ms" v={optionalDuration(status.pipeline.triage_p95_ms)} />
          <KeyValueRow
            k="queue_lag_p95_ms"
            v={optionalDuration(status.pipeline.queue_lag_p95_ms)}
          />
          <KeyValueRow k="e2e_p95_ms" v={optionalDuration(status.delivery.e2e_p95_ms)} />
          <KeyValueRow k="events_24h" v={String(status.pipeline.events_24h)} />
          <KeyValueRow k="candidates_24h" v={String(status.pipeline.candidates_24h)} />
          <KeyValueRow k="triage_24h" v={String(status.pipeline.triage_24h)} />
          <KeyValueRow k="triage_degraded_24h" v={String(status.pipeline.triage_degraded_24h)} />
          <KeyValueRow k="throttled_24h" v={String(status.pipeline.throttled_24h)} />
          {/* `all` is policy v7's content comparison path. `throttled` only
              decodes pre-v7 historical rows that lacked an explicit scope. */}
          <KeyValueRow
            k="duplicates_withheld_24h"
            v={`${status.pipeline.duplicates_withheld_24h?.throttled ?? 0} 历史记录 · ${
              status.pipeline.duplicates_withheld_24h?.all ?? 0
            } 全量比对`}
          />
          <KeyValueRow
            k="reviewed_should_push_24h"
            v={String(status.pipeline.reviewed_should_push_24h)}
          />
          <KeyValueRow
            k="reviewed_external_miss_24h"
            v={String(status.pipeline.reviewed_external_miss_24h)}
          />
          <KeyValueRow
            k="candidate_share_24h"
            v={String(status.pipeline.candidate_share_24h ?? "—")}
          />
          <KeyValueRow k="delivery.terminal_24h" v={String(status.delivery.terminal_24h)} />
          <KeyValueRow k="delivery.last_error_code" v={status.delivery.last_error_code ?? "—"} />
        </KeyValue>
      </section>
      <section>
        <h4>接入</h4>
        <KeyValue>
          <KeyValueRow k="connected" v={String(status.ingest.connected)} />
          <KeyValueRow k="token_configured" v={String(status.ingest.token_configured)} />
          <KeyValueRow k="last_frame_at_ms" v={optionalTime(status.ingest.last_frame_at_ms)} />
          <KeyValueRow k="last_publish_at_ms" v={optionalTime(status.ingest.last_publish_at_ms)} />
          <KeyValueRow k="last_error_code" v={status.ingest.last_error_code ?? "—"} />
          <KeyValueRow
            k="provider_enabled_strategy_count"
            v={String(status.ingest.provider_enabled_strategy_ids?.length ?? "—")}
          />
          <KeyValueRow
            k="configured_strategy_count"
            v={String((status.ingest.configured_strategy_ids ?? []).length)}
          />
          <KeyValueRow
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
        </KeyValue>
      </section>
      <section>
        <h4>队列</h4>
        <KeyValue>
          <KeyValueRow k="configured" v={String(status.broker.configured)} />
          <KeyValueRow k="connected" v={String(status.broker.connected ?? "unknown")} />
          <KeyValueRow k="observed_at_ms" v={optionalTime(status.broker.observed_at_ms)} />
          <KeyValueRow k="error_code" v={status.broker.error_code ?? "—"} />
          {queues.map(([name, queue]) => (
            <KeyValueRow
              k={name}
              key={name}
              v={`${queue.messages} 条 · ${queue.consumers} 消费者`}
            />
          ))}
        </KeyValue>
      </section>
      <section>
        <h4>学习证据保留</h4>
        <KeyValue>
          <KeyValueRow
            k="eligible"
            v={`${status.learning_retention.eligible_recordings} recordings · ${status.learning_retention.eligible_cases} cases · ${status.learning_retention.eligible_artifacts} artifacts`}
          />
          <KeyValueRow
            k="deleted_last_turn"
            v={`${status.learning_retention.deleted_recordings} recordings · ${status.learning_retention.deleted_cases} cases · ${status.learning_retention.deleted_artifacts} artifacts`}
          />
          <KeyValueRow
            k="oldest_recording_age"
            v={optionalDuration(status.learning_retention.oldest_recording_age_ms)}
          />
          <KeyValueRow
            k="oldest_case_age"
            v={optionalDuration(status.learning_retention.oldest_case_age_ms)}
          />
          <KeyValueRow
            k="oldest_artifact_age"
            v={optionalDuration(status.learning_retention.oldest_artifact_age_ms)}
          />
          <KeyValueRow
            k="last_run_at_ms"
            v={optionalTime(status.learning_retention.last_run_at_ms)}
          />
          <KeyValueRow k="last_error_code" v={status.learning_retention.last_error_code ?? "—"} />
        </KeyValue>
      </section>
    </NewsTechnical>
  );
}
