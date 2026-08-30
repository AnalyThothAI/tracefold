import { newsLeveragePath } from "@shared/routing/paths";
import { ActionButton } from "@shared/ui/ActionButton";
import { Card } from "@shared/ui/Card";
import { Metric, MetricRow } from "@shared/ui/Metric";
import * as PageState from "@shared/ui/PageState";
import { Link } from "react-router-dom";

import {
  type TradingAuthorityEvidence,
  type TradingCapitalLifecycleEvidence,
  type TradingCapabilityBinding,
  type TradingCapabilityEntry,
  useTradingCapabilitiesWithToken,
  useTradingEvidenceWithToken,
  useTradingIntentsWithToken,
  useTradingStatusWithToken,
  type TradingIntent,
} from "../api/tradingQueries";
import { INTENT_STATE_NOTE, caseClock, isActiveIntent, policyLabel } from "../model/tradingLabels";

import { TradingEmptyNote, TradingShell, TradingSourceLine } from "./TradingChrome";

import "./trading.css";

/**
 * Decision / Capital observer, immutable Intents, and what execution did with them (#350).
 *
 * Two reads, two aggregates, and no third. The Case list this page used to carry came from
 * `/api/trading/intents.cases_without_intents`, which meant a page about execution was also the page
 * about decisions — and an empty array from a failed request rendered as "the lane produced nothing".
 * Decisions live at 资本判定; this page links there rather than restating them.
 *
 * The four states are explicit. A cold failure is an error with a retry, a failed refresh keeps the last
 * answer and says so, and 0 Intent is a *truthful* empty that names the upstream figures beside it.
 */
export function TradingPage({ token }: { token: string }) {
  const statusQuery = useTradingStatusWithToken(token);
  const intentsQuery = useTradingIntentsWithToken(token);
  const capabilitiesQuery = useTradingCapabilitiesWithToken(token);
  const evidenceQuery = useTradingEvidenceWithToken(token);
  const status = statusQuery.data;
  const intents = intentsQuery.data?.intents ?? [];
  const active = intents.filter(isActiveIntent);
  const terminal = intents.filter((intent) => intent.execution_state === "TERMINAL");

  const coldFailure = statusQuery.isError && !status;
  return (
    <TradingShell label="Decision / Capital · 双场地 observer">
      <header className="trading-page-header">
        <div className="trading-heading-copy">
          <h1>Decision / Capital</h1>
          <p>决策面、资本控制与每个 execution binding 是三类独立事实。</p>
        </div>
        {status ? (
          <div className="trading-heading-aside" data-tone={runtimeTone(status)}>
            <span>{status.counts.day_key || "日期未知"} · UTC 预算日</span>
            <small>
              DECISION {status.decision.state} · CAPITAL {status.capital.control}
            </small>
          </div>
        ) : null}
      </header>

      {statusQuery.isLoading && !status ? (
        <PageState.Loading label="正在读取资本通道状态" layout="panel" rows={3} />
      ) : null}
      {coldFailure ? (
        <PageState.Error error={statusQuery.error} onRetry={() => void statusQuery.refetch()} />
      ) : null}

      {status ? (
        <PageState.Stale
          failedRefresh={
            intentsQuery.isError && !intentsQuery.data
              ? "Intent 账本读取失败，其余内容仍是上次读取。"
              : undefined
          }
          onRetry={() => void intentsQuery.refetch()}
          updating={statusQuery.isFetching || intentsQuery.isFetching}
        >
          <div className="trading-body">
            {observerNotice(status) ? (
              <Card title="当前资本状态">
                <p>{observerNotice(status)}</p>
              </Card>
            ) : null}
            <MetricRow className="trading-mandate" columns={6} label="独立运行事实">
              <Metric eyebrow="DECISION" value={status.decision.state} caption="策略决策面" />
              <Metric
                eyebrow="CAPITAL"
                value={status.capital.control}
                caption="新 economic Intent"
                tone={status.capital.control === "PAUSED" ? "caution" : undefined}
              />
              <Metric
                eyebrow="BINDINGS"
                value={`${configuredBindings(status)} / ${status.bindings.length}`}
                caption="凭证已配置"
              />
              <Metric
                eyebrow="NOTIONAL"
                value={`$${status.budget.target_notional_usd}`}
                caption="每次固定名义"
              />
              <Metric
                eyebrow="ENTRIES"
                value={`${status.counts.entries_today}`}
                caption="今日已入场"
              />
              <Metric
                eyebrow="ACTIVE"
                value={status.counts.active_intents}
                caption="非终态 Intent"
                tone={hasUnexpectedExposure(status) ? "caution" : "accent"}
              />
            </MetricRow>

            <BindingList status={status} />

            <CapabilitiesSection
              bindings={capabilitiesQuery.data?.pages[0]?.bindings ?? []}
              entries={capabilitiesQuery.data?.pages.flatMap((page) => page.entries ?? []) ?? []}
              error={capabilitiesQuery.error}
              failedRefresh={capabilitiesQuery.isError && Boolean(capabilitiesQuery.data)}
              hasNextPage={capabilitiesQuery.hasNextPage}
              loading={capabilitiesQuery.isPending}
              loadingMore={capabilitiesQuery.isFetchingNextPage}
              onLoadMore={() => void capabilitiesQuery.fetchNextPage()}
              onRetry={() => void capabilitiesQuery.refetch()}
              updating={capabilitiesQuery.isFetching}
            />

            <EvidenceSection
              authorities={evidenceQuery.data?.pages[0]?.authorities ?? []}
              error={evidenceQuery.error}
              failedRefresh={evidenceQuery.isError && Boolean(evidenceQuery.data)}
              hasNextPage={evidenceQuery.hasNextPage}
              lifecycles={evidenceQuery.data?.pages.flatMap((page) => page.lifecycles ?? []) ?? []}
              loading={evidenceQuery.isPending}
              loadingMore={evidenceQuery.isFetchingNextPage}
              onLoadMore={() => void evidenceQuery.fetchNextPage()}
              onRetry={() => void evidenceQuery.refetch()}
              updating={evidenceQuery.isFetching}
            />

            <IntentList
              empty={
                <>
                  当前没有非终态 Intent。过去 24 小时 Decision 成案 <b>{status.counts.cases_24h}</b>{" "}
                  个、形成 Intent <b>{status.counts.intents_24h}</b> 个；判定过程在{" "}
                  <Link to={newsLeveragePath()}>资本判定</Link>。
                </>
              }
              loading={intentsQuery.isPending}
              rows={active}
              title="Fresh / Active Intent"
            />
            <IntentList
              empty={<>当前窗口没有终态 Outcome。</>}
              loading={intentsQuery.isPending}
              rows={terminal}
              title="Terminal Outcome"
            />
            {intentsQuery.data?.complete === false ? (
              <p className="trading-pagination-note" role="status">
                Intent 列表仍有下一页；当前页面只陈述已读取的有界结果，不能把本页之外解释为空。
              </p>
            ) : null}

            <Card
              flush
              hint="执行阶段的终局分布，来自 durable 行的有界聚合"
              title="24h Outcome 分布"
            >
              {Object.entries(intentsQuery.data?.outcome_counts_24h ?? {}).length ? (
                <>
                  <Tally caption="终局状态" counts={intentsQuery.data?.outcome_counts_24h ?? {}} />
                  <Tally caption="终局原因" counts={intentsQuery.data?.reason_counts_24h ?? {}} />
                </>
              ) : (
                <TradingEmptyNote>
                  {intentsQuery.isError && !intentsQuery.data
                    ? "Intent 账本本轮不可用；不能据此断言没有终局。"
                    : "过去 24 小时没有终局 Outcome。"}
                </TradingEmptyNote>
              )}
              <TradingSourceLine path="GET /api/trading/intents → outcome_counts_24h · reason_counts_24h" />
            </Card>
          </div>
        </PageState.Stale>
      ) : null}
    </TradingShell>
  );
}

function CapabilitiesSection({
  bindings,
  entries,
  error,
  failedRefresh,
  hasNextPage,
  loading,
  loadingMore,
  onLoadMore,
  onRetry,
  updating,
}: {
  bindings: readonly TradingCapabilityBinding[];
  entries: readonly TradingCapabilityEntry[];
  error: unknown;
  failedRefresh: boolean;
  hasNextPage: boolean;
  loading: boolean;
  loadingMore: boolean;
  onLoadMore: () => void;
  onRetry: () => void;
  updating: boolean;
}) {
  if (loading && !bindings.length) {
    return (
      <PageState.Loading label="正在读取 execution capability partition" layout="panel" rows={3} />
    );
  }
  if (error && !bindings.length) return <PageState.Error error={error} onRetry={onRetry} />;
  return (
    <PageState.Stale
      failedRefresh={
        failedRefresh ? "Capability 刷新失败；保留上次读取，不能解释为当前空集。" : undefined
      }
      onRetry={onRetry}
      updating={updating}
    >
      <Card
        flush
        hint="每个 binding 的全量 included + excluded 守恒；compile error 与 last-known-good 分开"
        title="Execution capability partition"
      >
        <div className="trading-table">
          {bindings.map((binding) => (
            <article className="trading-capability-binding-row" key={binding.binding}>
              <b>{binding.binding}</b>
              <span data-tone={binding.capability_state === "ready" ? undefined : "caution"}>
                {binding.capability_state}
              </span>
              <span>
                {binding.catalog_instrument_count} = {binding.included_count} included +{" "}
                {binding.excluded_count} excluded
              </span>
              <span>{binding.last_known_good ? "last-known-good 有效" : "无可用快照"}</span>
              <code title={binding.snapshot_sha256 ?? undefined}>
                {binding.snapshot_sha256 ?? "snapshot —"}
              </code>
              <span>{binding.compile_error ?? "compile error —"}</span>
            </article>
          ))}
          {entries.map((entry) => (
            <article
              className="trading-capability-entry-row"
              key={`${entry.binding}:${entry.disposition}:${entry.catalog_entry_id}`}
            >
              <b>{entry.binding}</b>
              <span data-tone={entry.disposition === "included" ? undefined : "caution"}>
                {entry.disposition}
              </span>
              <code>{entry.provider_instrument_id}</code>
              <span>{entry.canonical_asset ?? "canonical —"}</span>
              <span>{entry.instrument_id ?? entry.exclusion_reason ?? "unproven"}</span>
            </article>
          ))}
        </div>
        {!bindings.length ? (
          <TradingEmptyNote>
            数据库没有 closed binding projection；这不是零 instrument 证明。
          </TradingEmptyNote>
        ) : !entries.length ? (
          <TradingEmptyNote>
            当前筛选没有 capability entry；binding summary 仍保留 missing/error/LKG 真相。
          </TradingEmptyNote>
        ) : null}
        <PaginationControl
          hasNextPage={hasNextPage}
          loading={loadingMore}
          onLoadMore={onLoadMore}
          subject="capability entries"
        />
        <TradingSourceLine path="GET /api/trading/capabilities → bindings[] · entries[] · next_cursor" />
      </Card>
    </PageState.Stale>
  );
}

function EvidenceSection({
  authorities,
  error,
  failedRefresh,
  hasNextPage,
  lifecycles,
  loading,
  loadingMore,
  onLoadMore,
  onRetry,
  updating,
}: {
  authorities: readonly TradingAuthorityEvidence[];
  error: unknown;
  failedRefresh: boolean;
  hasNextPage: boolean;
  lifecycles: readonly TradingCapitalLifecycleEvidence[];
  loading: boolean;
  loadingMore: boolean;
  onLoadMore: () => void;
  onRetry: () => void;
  updating: boolean;
}) {
  if (loading && !authorities.length) {
    return <PageState.Loading label="正在读取资本授权与风险证据" layout="panel" rows={3} />;
  }
  if (error && !authorities.length) return <PageState.Error error={error} onRetry={onRetry} />;
  return (
    <PageState.Stale
      failedRefresh={
        failedRefresh ? "证据刷新失败；保留上次读取，不能据此宣称 grant/arm 仍有效。" : undefined
      }
      onRetry={onRetry}
      updating={updating}
    >
      <Card
        flush
        hint="redacted PostgreSQL facts；absent / expired / revoked / invalid 均不是授权"
        title="Capital authority & lifecycle evidence"
      >
        <div className="trading-table">
          {authorities.map((authority) => (
            <article className="trading-authority-row" key={authority.binding}>
              <b>{authority.binding}</b>
              <span data-tone={authority.status === "active" ? undefined : "caution"}>
                {authority.status}
              </span>
              <span>{authority.approved_release ?? "release —"}</span>
              <code title={authority.grant_sha256 ?? undefined}>
                {authority.grant_sha256 ?? "grant —"}
              </code>
              <code title={authority.active_arm_receipt_sha256 ?? undefined}>
                {authority.active_arm_receipt_sha256 ?? "arm —"}
              </code>
              <span>
                {authority.arm_expires_at_ms == null
                  ? "arm expiry —"
                  : `arm ${caseClock(authority.arm_expires_at_ms)}`}
              </span>
              <span>
                {(authority.settlement_limits ?? []).length
                  ? (authority.settlement_limits ?? [])
                      .map(
                        (limit) =>
                          `${limit.settlement_asset} planned≤${limit.max_planned_risk_amount} loss≤${limit.max_realized_loss_amount}`,
                      )
                      .join(" · ")
                  : "risk limits —"}
              </span>
            </article>
          ))}
          {lifecycles.map((lifecycle) => (
            <article className="trading-lifecycle-row" key={lifecycle.reservation_sha256}>
              <b>{lifecycle.binding}</b>
              <code>{lifecycle.case_id}</code>
              <span>{lifecycle.risk_status}</span>
              <span>
                {lifecycle.execution_state}
                {lifecycle.execution_phase ? ` / ${lifecycle.execution_phase}` : ""}
              </span>
              <span>
                {lifecycle.current_planned_risk_amount} {lifecycle.settlement_asset}
              </span>
              <span>{lifecycle.attempt_consumed ? "attempt consumed" : "attempt unspent"}</span>
              <span>{lifecycle.terminal_outcome ?? lifecycle.reason_code ?? "nonterminal"}</span>
              <span>{lifecycle.settlement_known ? "settlement known" : "settlement unproven"}</span>
            </article>
          ))}
        </div>
        {!authorities.length ? (
          <TradingEmptyNote>授权 projection 不可见；不能将其解释为 active。</TradingEmptyNote>
        ) : !lifecycles.length ? (
          <TradingEmptyNote>
            当前有真实的零个资本生命周期；上方授权状态仍独立成立。
          </TradingEmptyNote>
        ) : null}
        <PaginationControl
          hasNextPage={hasNextPage}
          loading={loadingMore}
          onLoadMore={onLoadMore}
          subject="capital lifecycles"
        />
        <TradingSourceLine path="GET /api/trading/evidence → authorities[] · lifecycles[] · next_cursor" />
      </Card>
    </PageState.Stale>
  );
}

function PaginationControl({
  hasNextPage,
  loading,
  onLoadMore,
  subject,
}: {
  hasNextPage: boolean;
  loading: boolean;
  onLoadMore: () => void;
  subject: string;
}) {
  if (!hasNextPage) return null;
  return (
    <div className="trading-pagination-note" role="status">
      <span>{subject} 尚未读取完整；本页之外不能解释为空。</span>
      <ActionButton disabled={loading} onClick={onLoadMore} size="sm">
        {loading ? "正在读取" : "继续读取"}
      </ActionButton>
    </div>
  );
}

function BindingList({ status }: { status: TradingStatus }) {
  return (
    <Card
      flush
      hint="PostgreSQL durable projection；不含 secret、provider client 或推断 readiness"
      title="Execution bindings"
    >
      <div className="trading-table">
        {status.bindings.map((binding) => (
          <article className="trading-binding-row" key={binding.binding}>
            <b>{binding.binding}</b>
            <span>credentials {binding.credential_state}</span>
            <span>runtime {binding.runtime_state}</span>
            <span>account {binding.account_state}</span>
            <span>catalog {binding.catalog_state}</span>
            <code title={binding.catalog_snapshot_sha256 ?? undefined}>
              {binding.catalog_snapshot_sha256 ?? "digest —"}
            </code>
            <span>
              {binding.catalog_captured_at_ms == null
                ? "catalog time —"
                : caseClock(binding.catalog_captured_at_ms)}
            </span>
            <span>{binding.reason ?? "reason —"}</span>
          </article>
        ))}
      </div>
      <TradingSourceLine path="GET /api/trading/status → bindings[]" />
    </Card>
  );
}

/**
 * One bounded aggregate, keyed on its own dimension.
 *
 * The two 24h tallies are independent aggregates over the same window: one keyed on the terminal state,
 * one on the reason. They were rendered as one table, with the whole reason distribution repeated in
 * every outcome row — which reads as "these are CLOSED_FLAT's reasons" and is a join the server never
 * made. Two captioned lists say what each figure is counted by, and nothing more.
 */
function Tally({ caption, counts }: { caption: string; counts: Record<string, number> }) {
  const rows = Object.entries(counts);
  return (
    <dl className="trading-tally">
      <dt>{caption}</dt>
      {rows.length ? (
        rows.map(([key, count]) => (
          <dd key={key}>
            <code>{key}</code>
            <b>{count}</b>
          </dd>
        ))
      ) : (
        <dd>
          <span>—</span>
        </dd>
      )}
    </dl>
  );
}

function IntentList({
  empty,
  loading,
  rows,
  title,
}: {
  empty: React.ReactNode;
  loading: boolean;
  rows: readonly TradingIntent[];
  title: string;
}) {
  return (
    <Card flush hint="同一行只陈述账本已证明的 Intent 与 Outcome" title={title}>
      {loading ? (
        <PageState.Loading label="正在读取 Intent" layout="inline" rows={2} />
      ) : rows.length ? (
        <div className="trading-table">
          {rows.map((intent) => (
            <article className="trading-exposure-row" key={intent.intent_id}>
              <b>{intent.base_symbol}</b>
              <code>{intent.intent_id}</code>
              <span>{intent.execution_state}</span>
              <span>{intent.execution_phase ?? "等待领取"}</span>
              <span>{intent.terminal_outcome ?? INTENT_STATE_NOTE[intent.execution_state]}</span>
              <span>{outcome(intent)}</span>
              <span>{policyLabel(intent.policy_id)}</span>
              <span>{caseClock(intent.created_at_ms)}</span>
            </article>
          ))}
        </div>
      ) : (
        <TradingEmptyNote>{empty}</TradingEmptyNote>
      )}
      <TradingSourceLine path="GET /api/trading/intents → intents[]" />
    </Card>
  );
}

function outcome(intent: TradingIntent): string {
  if (intent.realized_pnl_amount == null) return intent.reason_code ?? "—";
  return `${intent.realized_pnl_amount} ${intent.realized_pnl_currency ?? ""}`.trim();
}

type TradingStatus = NonNullable<ReturnType<typeof useTradingStatusWithToken>["data"]>;

function configuredBindings(status: TradingStatus): number {
  return status.bindings.filter((binding) => binding.credential_state === "configured").length;
}

function hasUnexpectedExposure(status: TradingStatus): boolean {
  return status.bindings.some((binding) => binding.account_state === "exposure_present");
}

function observerNotice(status: TradingStatus): string | undefined {
  if (
    status.decision.state === "RUNNING" &&
    status.capital.control === "PAUSED" &&
    status.bindings.some((binding) => binding.credential_state === "unconfigured")
  ) {
    return "决策运行、资本暂停、凭证未配置，当前无法交易";
  }
  if (status.decision.state === "FAULTED")
    return `决策故障：${status.decision.reason ?? "原因未知"}`;
  if (hasUnexpectedExposure(status)) return "binding 报告未预期敞口；当前不能推断账户已平。";
  return undefined;
}

function runtimeTone(status: TradingStatus): "caution" | undefined {
  return status.decision.state === "RUNNING" && !hasUnexpectedExposure(status)
    ? undefined
    : "caution";
}
