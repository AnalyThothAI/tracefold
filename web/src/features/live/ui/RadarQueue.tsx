import {
  formatSignedPercent,
  formatTokenPriceUsd,
  formatUsdCompact,
  shortAddress,
} from "@lib/format";
import { gmgnTokenUrl } from "@lib/gmgn";
import { tokenTargetPath } from "@shared/routing/paths";
import { radarNavigationState } from "@shared/routing/radarNavigationState";
import * as PageState from "@shared/ui/PageState";
import { Button } from "@shared/ui/button";
import { useLayoutEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import {
  TOKEN_RADAR_WINDOW,
  type TokenRadarSnapshot,
  type TokenRadarSnapshotItem,
} from "../model/tokenRadarSnapshot";

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
  initialScrollTop?: number | null;
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
  initialScrollTop = null,
  onRetry,
  onSessionRetry,
  sessionAvailable,
}: RadarQueueProps) {
  const navigate = useNavigate();
  const itemsRef = useRef<HTMLOListElement>(null);
  const restoredScroll = useRef(false);

  useLayoutEffect(() => {
    if (restoredScroll.current || initialScrollTop === null || !snapshot) return;
    const items = itemsRef.current;
    if (!items) return;
    items.scrollTop = initialScrollTop;
    restoredScroll.current = true;
  }, [initialScrollTop, snapshot]);

  const openCase = (path: string) => {
    navigate(path, {
      state: radarNavigationState(itemsRef.current?.scrollTop ?? 0),
    });
  };

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
    <section className="live-radar-queue" aria-label="Radar" aria-busy={isRefreshing}>
      <RadarHeader snapshot={snapshot} />
      <div className="live-radar-content">
        <ol className="live-radar-items" aria-label="Radar priority queue" ref={itemsRef}>
          <RadarQueueItem item={items[0] ?? null} key="radar-first-slot" onOpen={openCase} />
          {items.slice(1).map((item) => (
            <RadarQueueItem
              item={item}
              key={`${item.target.target_type}:${item.target.target_id}`}
              onOpen={openCase}
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
      <span className="live-radar-method">
        {TOKEN_RADAR_WINDOW} causal change · newest qualification first
      </span>
      {snapshot ? (
        <span className="live-radar-count">
          {`${snapshot.eligible_total} eligible · showing ${snapshot.items.length} / ${TOKEN_RADAR_MAX_ITEMS}`}
        </span>
      ) : null}
      {snapshot ? (
        <time
          dateTime={
            snapshot.social_evidence_as_of_ms > 0
              ? new Date(snapshot.social_evidence_as_of_ms).toISOString()
              : undefined
          }
        >
          {snapshot.social_evidence_as_of_ms > 0
            ? `Social evidence through ${formatTimestamp(snapshot.social_evidence_as_of_ms)}`
            : "No social evidence yet"}
        </time>
      ) : null}
    </header>
  );
}

function RadarQueueItem({
  item,
  onOpen,
}: {
  item: TokenRadarSnapshotItem | null;
  onOpen: (path: string) => void;
}) {
  const [failedLogoUrl, setFailedLogoUrl] = useState<string | null>(null);
  const [copyResult, setCopyResult] = useState<{
    address: string;
    status: "copying" | "copied" | "failed";
  } | null>(null);
  const empty = item === null;
  const symbol = item?.target.symbol ?? "";
  const logoUrl = item?.target.logo_url ?? null;
  const identity = item ? formatIdentity(item) : "";
  const address = item?.target.address?.trim() || null;
  const addressOnly =
    item?.target.target_type === "Asset" && address !== null && symbol === address;
  const identityLabel = addressOnly && address ? shortAddress(address) : symbol;
  const displayName = item?.target.name ?? (addressOnly ? "Contract address" : symbol);
  const interactiveAddress = empty ? null : address;
  const gmgnUrl = item ? gmgnTokenUrl(item.target.chain, address) : null;
  const copyStatus = copyResult?.address === interactiveAddress ? copyResult.status : "idle";
  const change = item?.market.price_change_since_signal ?? null;
  const casePath = item
    ? tokenTargetPath({
        targetType: item.target.target_type,
        targetId: item.target.target_id,
        window: TOKEN_RADAR_WINDOW,
        focus: "trigger",
        triggerEventId: item.trigger_event_id,
      })
    : "/";
  return (
    <li className="live-radar-item" role={empty ? "presentation" : undefined}>
      <div className="live-radar-identity">
        <span className="live-radar-icon" style={empty ? { visibility: "hidden" } : undefined}>
          <span className="live-radar-icon-fallback" aria-hidden>
            {identityLabel.slice(0, 1).toUpperCase()}
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
            {empty ? "No eligible cases" : addressOnly ? identityLabel : `$${symbol}`}
          </strong>
          <span title={empty ? undefined : `${displayName} · ${identity}`}>
            {empty ? "Waiting for fixed evidence rules" : displayName}
          </span>
          <small className="live-radar-contract">
            {gmgnUrl ? (
              <a
                aria-label={`Open ${identityLabel} on GMGN`}
                href={gmgnUrl}
                rel="noreferrer"
                target="_blank"
                title={address ?? undefined}
              >
                <span>{formatContractIdentity(identity, address ?? "")}</span>
                <span className="live-radar-gmgn"> · GMGN ↗</span>
              </a>
            ) : (
              <span>
                {empty
                  ? "\u00a0"
                  : address
                    ? formatContractIdentity(identity, address)
                    : identity || "Unknown venue"}
              </span>
            )}
            <button
              aria-atomic="true"
              aria-hidden={!interactiveAddress || undefined}
              aria-label={
                !interactiveAddress
                  ? undefined
                  : copyStatus === "copied"
                    ? `${identityLabel} contract address copied`
                    : copyStatus === "failed"
                      ? `${identityLabel} contract address copy failed`
                      : copyStatus === "copying"
                        ? `${identityLabel} contract address copying`
                        : `Copy ${identityLabel} contract address`
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
        aria-label={empty ? undefined : `${identityLabel} market facts`}
        className="live-radar-market"
        style={empty ? { visibility: "hidden" } : undefined}
      >
        <span
          aria-label={`Price ${formatPrice(item?.market.price_usd ?? null)}, ${formatObservation(item?.market.price_observed_at_ms ?? null)}`}
          data-label="Price"
          role="group"
        >
          <span className="live-radar-market-reading">
            <span>{formatPrice(item?.market.price_usd ?? null)}</span>
            <MarketObservation
              label="Price observation time"
              value={item?.market.price_observed_at_ms ?? null}
            />
          </span>
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
          <span className="live-radar-market-reading">
            <span>{formatChange(change)}</span>
          </span>
        </span>
        <span
          aria-label={`Market cap ${formatMarketCap(item?.market.market_cap_usd ?? null)}, ${formatObservation(item?.market.market_cap_observed_at_ms ?? null)}`}
          data-label="Market cap"
          role="group"
        >
          <span className="live-radar-market-reading">
            <span>{formatMarketCap(item?.market.market_cap_usd ?? null)}</span>
            <MarketObservation
              label="Market-cap observation time"
              value={item?.market.market_cap_observed_at_ms ?? null}
            />
          </span>
        </span>
      </div>
      <div
        aria-hidden={empty || undefined}
        aria-label={empty ? undefined : `${identityLabel} evidence`}
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
          aria-label={`Independent evidence, ${item?.evidence.independent_author_count ?? 0} independent authors, ${item?.evidence.independent_text_count ?? 0} independent texts`}
          data-label="Independent evidence"
          role="group"
        >
          {`${item?.evidence.independent_author_count ?? 0} authors · ${item?.evidence.independent_text_count ?? 0} texts`}
        </span>
        <span
          aria-label={`Formation quality, formed in ${formatDuration(item?.evidence.time_to_nth_author_ms ?? 0)}, ${formatPercent(item?.evidence.duplicate_share ?? 0)} duplicate text`}
          data-label="Formation"
          role="group"
        >
          {`${formatDuration(item?.evidence.time_to_nth_author_ms ?? 0)} to form · ${formatPercent(item?.evidence.duplicate_share ?? 0)} duplicate`}
        </span>
        <time
          dateTime={item ? new Date(item.trigger_source_event_at_ms).toISOString() : undefined}
          title="Trigger source-event time"
        >
          {item ? `Source ${RADAR_TRIGGER_FORMATTER.format(item.trigger_source_event_at_ms)}` : ""}
        </time>
        <time
          dateTime={item ? new Date(item.qualified_at_ms).toISOString() : undefined}
          title="Qualification time"
        >
          {item ? `Qualified ${RADAR_TRIGGER_FORMATTER.format(item.qualified_at_ms)}` : ""}
        </time>
      </div>
      <Link
        aria-label={empty ? undefined : `Open ${identityLabel} Token Case`}
        aria-hidden={empty || undefined}
        className="live-radar-card-link"
        onClick={(event) => {
          if (
            empty ||
            event.button !== 0 ||
            event.metaKey ||
            event.ctrlKey ||
            event.shiftKey ||
            event.altKey
          ) {
            return;
          }
          event.preventDefault();
          onOpen(casePath);
        }}
        style={empty ? { visibility: "hidden" } : undefined}
        tabIndex={empty ? -1 : undefined}
        to={casePath}
      >
        <span aria-hidden>Open Token Case</span>
      </Link>
    </li>
  );
}

function MarketObservation({ label, value }: { label: string; value: number | null }) {
  return (
    <small>
      {value === null ? (
        "No observation"
      ) : (
        <time dateTime={new Date(value).toISOString()} title={label}>
          {formatObservation(value)}
        </time>
      )}
    </small>
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

function formatObservation(value: number | null): string {
  return value === null ? "No observation" : `Observed ${RADAR_TRIGGER_FORMATTER.format(value)}`;
}

function formatTimestamp(value: number): string {
  return RADAR_TIMESTAMP_FORMATTER.format(value);
}
