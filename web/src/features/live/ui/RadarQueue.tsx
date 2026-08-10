import { tokenTargetPath } from "@shared/routing/paths";
import * as PageState from "@shared/ui/PageState";
import { Button } from "@shared/ui/button";
import { Link } from "react-router-dom";

import type { TokenRadarSnapshot, TokenRadarSnapshotItem } from "../model/tokenRadarSnapshot";

import "./live.css";

const MARKET_PERCENTAGE_FORMATTER = new Intl.NumberFormat("en", { maximumFractionDigits: 1 });
const RADAR_TIMESTAMP_FORMATTER = new Intl.DateTimeFormat("zh-CN", {
  dateStyle: "short",
  timeStyle: "medium",
  hour12: false,
});
const RADAR_SLOT_INDEXES = Array.from({ length: 8 }, (_, index) => index);

type RadarQueueProps = {
  bootstrapError: boolean;
  bootstrapLoading: boolean;
  snapshot: TokenRadarSnapshot | null;
  error: Error | null;
  isLoading: boolean;
  isRefreshing: boolean;
  onRetry: () => void;
  onSessionRetry: () => void;
  sessionAvailable: boolean;
};

export function RadarQueue({
  bootstrapError,
  bootstrapLoading,
  snapshot,
  error,
  isLoading,
  isRefreshing,
  onRetry,
  onSessionRetry,
  sessionAvailable,
}: RadarQueueProps) {
  if (bootstrapLoading) {
    return (
      <section className="live-radar-queue" aria-label="Radar">
        <RadarHeader snapshot={null} />
        <PageState.Loading layout="panel" rows={3} label="establishing Radar read session" />
      </section>
    );
  }
  if (bootstrapError) {
    return (
      <section className="live-radar-queue" aria-label="Radar">
        <RadarHeader snapshot={null} />
        <PageState.Error
          error={new Error("Radar read session could not be established.")}
          onRetry={onSessionRetry}
        />
      </section>
    );
  }
  if (!sessionAvailable) {
    return (
      <section className="live-radar-queue" aria-label="Radar">
        <RadarHeader snapshot={null} />
        <PageState.Empty
          action={
            <Button onClick={onSessionRetry} size="sm" type="button" variant="outline">
              Reload
            </Button>
          }
          hint="Bootstrap completed without an access token; refresh the page or check service authentication."
          title="Radar read session unavailable"
        />
      </section>
    );
  }
  if (!snapshot && isLoading) {
    return (
      <section className="live-radar-queue" aria-label="Radar">
        <RadarHeader snapshot={null} />
        <PageState.Loading layout="panel" rows={8} label="loading Radar" />
      </section>
    );
  }
  if (!snapshot && error) {
    return (
      <section className="live-radar-queue" aria-label="Radar">
        <RadarHeader snapshot={null} />
        <PageState.Error error={error} onRetry={onRetry} />
      </section>
    );
  }
  const items = snapshot?.items ?? [];

  return (
    <section
      className={`live-radar-queue${snapshot && error ? " live-radar-queue--delayed" : ""}`}
      aria-label="Radar"
      aria-busy={isRefreshing}
    >
      <RadarHeader snapshot={snapshot} />
      {snapshot && error ? (
        <p className="live-radar-delay" role="status">
          更新延迟
        </p>
      ) : null}
      <div className="live-radar-content">
        <ol className="live-radar-items" aria-label="Radar priority queue">
          <li
            aria-hidden={items.length > 0 ? true : undefined}
            className={`live-radar-empty${items.length > 0 ? " live-radar-empty--inactive" : ""}`}
            inert={items.length > 0 ? true : undefined}
            role="presentation"
            style={{ visibility: items.length > 0 ? "hidden" : "visible" }}
          >
            <PageState.Empty title="No eligible cases" />
          </li>
          {RADAR_SLOT_INDEXES.map((index) => (
            <RadarQueueItem item={items[index] ?? null} key={index} />
          ))}
        </ol>
      </div>
    </section>
  );
}

function RadarHeader({ snapshot }: { snapshot: TokenRadarSnapshot | null }) {
  return (
    <header className="live-radar-header">
      <h1>Radar</h1>
      {snapshot ? <span>{snapshot.eligible_total} eligible</span> : null}
      {snapshot ? (
        <time
          dateTime={
            snapshot.evidence_as_of_ms > 0
              ? new Date(snapshot.evidence_as_of_ms).toISOString()
              : undefined
          }
        >
          {snapshot.evidence_as_of_ms > 0
            ? formatTimestamp(snapshot.evidence_as_of_ms)
            : "no evidence"}
        </time>
      ) : null}
    </header>
  );
}

function RadarQueueItem({ item }: { item: TokenRadarSnapshotItem | null }) {
  const primaryText = item ? formatPrimaryLine(item) : "";
  const evidenceText = item ? formatEvidenceLine(item) : "";
  const casePath = item
    ? tokenTargetPath({
        targetType: item.target.target_type,
        targetId: item.target.target_id,
        window: "1h",
        focus: "trigger",
        triggerEventId: item.trigger_event_id,
      })
    : ".";
  return (
    <li
      aria-hidden={item ? undefined : true}
      className={`live-radar-item${item ? "" : " live-radar-item--empty"}`}
      inert={item ? undefined : true}
      style={{ minHeight: 75, visibility: item ? "visible" : "hidden" }}
    >
      <div className="live-radar-item-primary">
        <span aria-label={primaryText || undefined} title={primaryText || undefined}>
          {primaryText}
        </span>
        <Link tabIndex={item ? undefined : -1} to={casePath}>
          Open Token Case
        </Link>
      </div>
      <div className="live-radar-item-evidence">{evidenceText}</div>
    </li>
  );
}

function formatPrimaryLine(item: TokenRadarSnapshotItem): string {
  const exchange =
    item.target.exchange && item.target.exchange !== item.target.chain
      ? item.target.exchange
      : null;
  const identity = [item.target.chain, exchange, shortIdentity(item.target.address ?? "")]
    .filter(Boolean)
    .join(" · ");
  return [
    formatSigned(item.why_now.mention_delta),
    `$${item.target.symbol}`,
    identity,
    formatMarket(item),
    formatTimestamp(item.triggered_at_ms),
  ]
    .filter(Boolean)
    .join(" · ");
}

function formatEvidenceLine(item: TokenRadarSnapshotItem): string {
  const counterEvidence = item.counter_evidence ? "counter: market confirmation unavailable" : null;
  return [
    `mentions ${item.why_now.prior_mentions}→${item.why_now.current_mentions}`,
    `${item.evidence.new_independent_author_count} new authors`,
    `${item.evidence.independent_text_count} independent texts`,
    `formed in ${formatDuration(item.evidence.time_to_nth_author_ms)}`,
    `${formatPercent(item.evidence.duplicate_share)} duplicates`,
    counterEvidence,
  ]
    .filter(Boolean)
    .join(" · ");
}

function shortIdentity(value: string): string {
  return value.length > 18 ? `${value.slice(0, 8)}…${value.slice(-6)}` : value;
}

function formatSigned(value: number): string {
  return value > 0 ? `+${value}` : String(value);
}

function formatMarket(item: TokenRadarSnapshotItem): string {
  if (item.market.status === "unavailable" || item.market.price_change_since_signal === null) {
    return "market unavailable";
  }
  const percentage = item.market.price_change_since_signal * 100;
  const formatted = MARKET_PERCENTAGE_FORMATTER.format(Math.abs(percentage));
  const sign = percentage > 0 ? "+" : percentage < 0 ? "−" : "";
  return `${sign}${formatted}% since signal`;
}

function formatPercent(value: number): string {
  return `${Math.round(value * 100)}%`;
}

function formatDuration(value: number): string {
  if (value < 60_000) return `${Math.round(value / 1_000)}s`;
  return `${Math.round(value / 60_000)}m`;
}

function formatTimestamp(value: number): string {
  return RADAR_TIMESTAMP_FORMATTER.format(value);
}
