import {
  formatSignedPercent,
  formatTokenPriceUsd,
  formatUsdCompact,
  shortAddress,
} from "@lib/format";
import { gmgnTokenUrl } from "@lib/gmgn";
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
const RADAR_TRIGGER_FORMATTER = new Intl.DateTimeFormat("zh-CN", {
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
});
const TOKEN_RADAR_MAX_ITEMS = 50;

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
      <span className="live-radar-method">1h acceleration · newest trigger first</span>
      {snapshot ? (
        <span className="live-radar-count">
          {`${snapshot.eligible_total} eligible · showing ${snapshot.items.length} / ${TOKEN_RADAR_MAX_ITEMS}`}
        </span>
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
            ? `Evidence through ${formatTimestamp(snapshot.evidence_as_of_ms)}`
            : "No evidence yet"}
        </time>
      ) : null}
    </header>
  );
}

function RadarQueueItem({ item }: { item: TokenRadarSnapshotItem | null }) {
  const [failedLogoUrl, setFailedLogoUrl] = useState<string | null>(null);
  const [copyResult, setCopyResult] = useState<{
    address: string;
    status: "copying" | "copied" | "failed";
  } | null>(null);
  const empty = item === null;
  const symbol = item?.target.symbol ?? "";
  const displayName = item?.target.name ?? symbol;
  const logoUrl = item?.target.logo_url ?? null;
  const identity = item ? formatIdentity(item) : "";
  const address = item?.target.address?.trim() || null;
  const interactiveAddress = empty ? null : address;
  const gmgnUrl = item ? gmgnTokenUrl(item.target.chain, address) : null;
  const copyStatus = copyResult?.address === interactiveAddress ? copyResult.status : "idle";
  const change = item?.market.price_change_since_signal ?? null;
  const casePath = item
    ? tokenTargetPath({
        targetType: item.target.target_type,
        targetId: item.target.target_id,
        window: "1h",
        focus: "trigger",
        triggerEventId: item.trigger_event_id,
      })
    : "/";
  return (
    <li className="live-radar-item" role={empty ? "presentation" : undefined}>
      <div className="live-radar-identity">
        <span className="live-radar-icon" style={empty ? { visibility: "hidden" } : undefined}>
          <span className="live-radar-icon-fallback" aria-hidden>
            {symbol.slice(0, 1).toUpperCase()}
          </span>
          <img
            alt={empty ? "" : `${displayName} icon`}
            decoding="async"
            hidden={logoUrl === null || failedLogoUrl === logoUrl}
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
          <strong role={empty ? "status" : undefined}>
            {empty ? "No eligible cases" : `$${symbol}`}
          </strong>
          <span title={empty ? undefined : `${displayName} · ${identity}`}>
            {empty ? "Waiting for fixed evidence rules" : displayName}
          </span>
          <small className="live-radar-contract">
            <a
              aria-hidden={empty || undefined}
              aria-label={!empty && gmgnUrl ? `Open ${symbol} on GMGN` : undefined}
              href={gmgnUrl ?? undefined}
              rel={gmgnUrl ? "noreferrer" : undefined}
              tabIndex={!empty && gmgnUrl ? undefined : -1}
              target={gmgnUrl ? "_blank" : undefined}
              title={address ?? undefined}
            >
              <span>
                {empty
                  ? "\u00a0"
                  : address
                    ? formatContractIdentity(identity, address)
                    : identity || "Unknown venue"}
              </span>
              {gmgnUrl ? <span className="live-radar-gmgn"> · GMGN ↗</span> : null}
            </a>
            <button
              aria-atomic="true"
              aria-hidden={!interactiveAddress || undefined}
              aria-label={
                !interactiveAddress
                  ? undefined
                  : copyStatus === "copied"
                    ? `${symbol} contract address copied`
                    : copyStatus === "failed"
                      ? `${symbol} contract address copy failed`
                      : copyStatus === "copying"
                        ? `${symbol} contract address copying`
                        : `Copy ${symbol} contract address`
              }
              aria-live="polite"
              disabled={!interactiveAddress || copyStatus === "copying"}
              onClick={() => {
                if (!interactiveAddress) return;
                setCopyResult({ address: interactiveAddress, status: "copying" });
                void copyContractAddress(interactiveAddress).then((copied) => {
                  setCopyResult((current) =>
                    current?.address === interactiveAddress && current.status === "copying"
                      ? {
                          address: interactiveAddress,
                          status: copied ? "copied" : "failed",
                        }
                      : current,
                  );
                });
              }}
              style={!interactiveAddress ? { visibility: "hidden" } : undefined}
              tabIndex={interactiveAddress ? undefined : -1}
              type="button"
            >
              {copyStatus === "copied"
                ? "Copied"
                : copyStatus === "failed"
                  ? "Copy failed"
                  : copyStatus === "copying"
                    ? "Copying…"
                    : "Copy"}
            </button>
          </small>
        </span>
      </div>
      <div
        aria-hidden={empty || undefined}
        aria-label={empty ? undefined : `${symbol} market facts`}
        className="live-radar-market"
        style={empty ? { visibility: "hidden" } : undefined}
      >
        <span
          aria-label={`Price ${formatPrice(item?.market.price_usd ?? null)}`}
          data-label="Price"
          role="group"
        >
          {formatPrice(item?.market.price_usd ?? null)}
        </span>
        <span
          aria-label={`Since signal ${formatChange(change)}`}
          className={
            change === null
              ? undefined
              : change > 0
                ? "is-positive"
                : change < 0
                  ? "is-negative"
                  : undefined
          }
          data-label="Since signal"
          role="group"
        >
          {formatChange(change)}
        </span>
        <span
          aria-label={`Market cap ${formatMarketCap(item?.market.market_cap_usd ?? null)}`}
          data-label="Market cap"
          role="group"
        >
          {formatMarketCap(item?.market.market_cap_usd ?? null)}
        </span>
      </div>
      <div
        aria-hidden={empty || undefined}
        aria-label={empty ? undefined : `${symbol} evidence`}
        className="live-radar-item-evidence"
        style={empty ? { visibility: "hidden" } : undefined}
      >
        <span
          aria-label={`Mentions ${item?.why_now.prior_mentions ?? 0} to ${item?.why_now.current_mentions ?? 0}, increase ${item?.why_now.mention_delta ?? 0}`}
          data-label="Mentions"
          role="group"
        >
          {`${item?.why_now.prior_mentions ?? 0}→${item?.why_now.current_mentions ?? 0} · ${formatSigned(item?.why_now.mention_delta ?? 0)}`}
        </span>
        <span
          aria-label={`New evidence, ${item?.evidence.new_independent_author_count ?? 0} new authors, ${item?.evidence.independent_text_count ?? 0} independent texts`}
          data-label="New evidence"
          role="group"
        >
          {`${item?.evidence.new_independent_author_count ?? 0} authors · ${item?.evidence.independent_text_count ?? 0} texts`}
        </span>
        <span
          aria-label={`Formation quality, formed in ${formatDuration(item?.evidence.time_to_nth_author_ms ?? 0)}, ${formatPercent(item?.evidence.duplicate_share ?? 0)} duplicate text`}
          data-label="Formation"
          role="group"
        >
          {`${formatDuration(item?.evidence.time_to_nth_author_ms ?? 0)} to form · ${formatPercent(item?.evidence.duplicate_share ?? 0)} duplicate`}
        </span>
        <time
          dateTime={item ? new Date(item.triggered_at_ms).toISOString() : undefined}
          title="Signal trigger time"
        >
          {item ? `Triggered ${RADAR_TRIGGER_FORMATTER.format(item.triggered_at_ms)}` : ""}
        </time>
        {item?.counter_evidence ? (
          <span className="live-radar-counter">Market confirmation unavailable</span>
        ) : null}
      </div>
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
  if (item.target.target_type === "Asset") {
    return chainDisplayName(item.target.chain);
  }
  return item.target.exchange?.trim() || "CEX";
}

function chainDisplayName(chain: string | null): string {
  if (!chain) return "Unknown chain";
  return CHAIN_DISPLAY_NAMES.get(chain.trim().toLowerCase()) ?? chain;
}

const CHAIN_DISPLAY_NAMES = new Map([
  ["eip155:1", "Ethereum"],
  ["eip155:56", "BNB Chain"],
  ["eip155:8453", "Base"],
  ["robinhood", "Robinhood Chain"],
  ["solana", "Solana"],
]);

function formatContractIdentity(identity: string, address: string): string {
  return [identity, shortAddress(address)].filter(Boolean).join(" · ");
}

async function copyContractAddress(address: string): Promise<boolean> {
  if (!navigator.clipboard?.writeText) return false;
  try {
    await navigator.clipboard.writeText(address);
    return true;
  } catch {
    return false;
  }
}

function formatSigned(value: number): string {
  return value > 0 ? `+${value}` : String(value);
}

function formatPrice(value: number | null): string {
  return value === null ? "No fresh quote" : formatTokenPriceUsd(value);
}

function formatChange(value: number | null): string {
  return value === null ? "No signal change" : formatSignedPercent(value);
}

function formatMarketCap(value: number | null): string {
  return value === null ? "No fresh cap" : formatUsdCompact(value);
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
