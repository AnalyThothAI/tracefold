import {
  formatSignedPercent,
  formatTokenPriceUsd,
  formatUsdCompact,
  shortAddress,
} from "@lib/format";
import { tokenTargetPath } from "@shared/routing/paths";
import * as PageState from "@shared/ui/PageState";
import { Button } from "@shared/ui/button";
import { useState } from "react";
import { Link } from "react-router-dom";

import type { TokenRadarSnapshot, TokenRadarSnapshotItem } from "../model/tokenRadarSnapshot";

import "./live.css";

const RADAR_TIMESTAMP_FORMATTER = new Intl.DateTimeFormat("zh-CN", {
  dateStyle: "short",
  timeStyle: "medium",
  hour12: false,
});

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
          <RadarQueueItem item={items[0] ?? null} key="radar-first-slot" />
          {items.slice(1).map((item) => (
            <RadarQueueItem
              item={item}
              key={`${item.target.target_type}:${item.target.target_id}`}
            />
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
      {snapshot ? (
        <span>{`Showing ${snapshot.items.length} / ${snapshot.eligible_total} eligible`}</span>
      ) : null}
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
  const [failedLogoUrl, setFailedLogoUrl] = useState<string | null>(null);
  const empty = item === null;
  const symbol = item?.target.symbol ?? "";
  const displayName = item?.target.name ?? symbol;
  const logoUrl = item?.target.logo_url ?? null;
  const identity = item === null ? "" : formatIdentity(item);
  const change = item?.market.price_change_since_signal ?? null;
  const casePath =
    item === null
      ? "/"
      : tokenTargetPath({
          targetType: item.target.target_type,
          targetId: item.target.target_id,
          window: "1h",
          focus: "trigger",
          triggerEventId: item.trigger_event_id,
        });
  return (
    <li className="live-radar-item">
      <div className="live-radar-identity">
        <span className="live-radar-icon" style={empty ? { visibility: "hidden" } : undefined}>
          <span className="live-radar-icon-fallback" aria-hidden>
            {symbol.slice(0, 1).toUpperCase() || "\u00a0"}
          </span>
          <img
            alt={empty ? "" : `${displayName} icon`}
            decoding="async"
            hidden={empty || logoUrl === null || failedLogoUrl === logoUrl}
            loading="lazy"
            onError={() => {
              setFailedLogoUrl(logoUrl);
            }}
            onLoad={() => {
              setFailedLogoUrl(null);
            }}
            src={logoUrl ?? undefined}
          />
        </span>
        <span className="live-radar-token-copy">
          <strong>{empty ? "No eligible cases" : `$${symbol}`}</strong>
          <span title={empty ? undefined : `${displayName} · ${identity}`}>
            {empty ? "\u00a0" : displayName}
          </span>
          <small>{identity || "\u00a0"}</small>
        </span>
      </div>
      <div
        className="live-radar-market"
        aria-hidden={empty || undefined}
        aria-label={empty ? undefined : `${symbol} market facts`}
        style={empty ? { visibility: "hidden" } : undefined}
      >
        <span>{empty ? "\u00a0" : `Price ${formatPrice(item.market.price_usd)}`}</span>
        <span
          className={
            empty || change === null
              ? undefined
              : change > 0
                ? "is-positive"
                : change < 0
                  ? "is-negative"
                  : undefined
          }
        >
          {empty ? "\u00a0" : `${formatChange(change)} since signal`}
        </span>
        <span>{empty ? "\u00a0" : `MCap ${formatMarketCap(item.market.market_cap_usd)}`}</span>
      </div>
      <p
        className="live-radar-item-evidence"
        aria-hidden={empty || undefined}
        style={empty ? { visibility: "hidden" } : undefined}
      >
        {empty ? "\u00a0" : formatEvidenceLine(item)}
      </p>
      <Link
        aria-hidden={empty || undefined}
        style={empty ? { visibility: "hidden" } : undefined}
        tabIndex={empty ? -1 : undefined}
        to={casePath}
      >
        Open Token Case
      </Link>
    </li>
  );
}

function formatIdentity(item: TokenRadarSnapshotItem): string {
  const exchange =
    item.target.exchange && item.target.exchange !== item.target.chain
      ? item.target.exchange
      : null;
  return [
    item.target.chain,
    exchange,
    item.target.address ? shortAddress(item.target.address) : null,
  ]
    .filter(Boolean)
    .join(" · ");
}

function formatEvidenceLine(item: TokenRadarSnapshotItem): string {
  const counterEvidence = item.counter_evidence ? "counter: market confirmation unavailable" : null;
  return [
    `${formatSigned(item.why_now.mention_delta)} mentions`,
    `${item.why_now.prior_mentions}→${item.why_now.current_mentions}`,
    `${item.evidence.new_independent_author_count} new authors`,
    `${item.evidence.independent_text_count} independent texts`,
    `formed in ${formatDuration(item.evidence.time_to_nth_author_ms)}`,
    `${formatPercent(item.evidence.duplicate_share)} duplicates`,
    formatTimestamp(item.triggered_at_ms),
    counterEvidence,
  ]
    .filter(Boolean)
    .join(" · ");
}

function formatSigned(value: number): string {
  return value > 0 ? `+${value}` : String(value);
}

function formatPrice(value: number | null): string {
  return value === null ? "—" : formatTokenPriceUsd(value);
}

function formatChange(value: number | null): string {
  return value === null ? "—" : formatSignedPercent(value);
}

function formatMarketCap(value: number | null): string {
  return value === null ? "—" : formatUsdCompact(value);
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
