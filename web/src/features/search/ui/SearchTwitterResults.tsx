import {
  formatRelativeTime,
  formatScore,
  formatSignedPercent,
  formatTokenPriceUsd,
} from "@lib/format";
import type { SearchItem, TokenReference, TokenTimelinePost } from "@lib/types";
import * as PageState from "@shared/ui/PageState";
import { useMemo, useState } from "react";

type SearchTwitterResultsProps = {
  title?: string;
  posts?: TokenTimelinePost[];
  items?: SearchItem[];
  selectedStageId?: string;
  hasMore?: boolean;
  onSelectedStageChange?: (stageId: string) => void;
};

type EvidenceRow = {
  id: string;
  receivedAtMs?: number | null;
  phase: string;
  stageId?: string | null;
  handle?: string | null;
  text: string;
  anchor: string;
  quality?: number | null;
  delta?: number | null;
  url?: string | null;
};

export function SearchTwitterResults({
  title = "Evidence Stream",
  posts = [],
  items = [],
  selectedStageId = "all",
  hasMore = false,
  onSelectedStageChange,
}: SearchTwitterResultsProps) {
  const [query, setQuery] = useState("");
  const [sortMode, setSortMode] = useState<"recent" | "quality">("recent");

  const rows = useMemo(
    () => (posts.length ? posts.map(rowFromPost) : items.map(rowFromSearchItem)),
    [items, posts],
  );
  const stages = useMemo(() => stageOptions(rows), [rows]);
  const filteredRows = useMemo(() => {
    const normalizedQuery = query.trim().toLowerCase();
    return rows
      .filter((row) => selectedStageId === "all" || row.stageId === selectedStageId)
      .filter(
        (row) =>
          !normalizedQuery ||
          row.text.toLowerCase().includes(normalizedQuery) ||
          (row.handle ?? "").toLowerCase().includes(normalizedQuery) ||
          row.id.toLowerCase().includes(normalizedQuery),
      )
      .sort((left, right) =>
        sortMode === "quality"
          ? Number(right.quality ?? -1) - Number(left.quality ?? -1)
          : Number(right.receivedAtMs ?? 0) - Number(left.receivedAtMs ?? 0),
      );
  }, [query, rows, selectedStageId, sortMode]);

  return (
    <section className="search-panel search-twitter-results" id="evidence">
      <header>
        <h3>{title}</h3>
        <span>
          {filteredRows.length}/{rows.length} rows{hasMore ? " · more available" : ""}
        </span>
      </header>

      <div className="search-evidence-toolbar">
        <input
          aria-label="filter evidence"
          onChange={(event) => setQuery(event.target.value)}
          placeholder="filter text / handle / event id"
          value={query}
        />
        <select
          aria-label="stage filter"
          onChange={(event) => onSelectedStageChange?.(event.target.value)}
          value={selectedStageId}
        >
          <option value="all">all stages</option>
          {stages.map((stage) => (
            <option key={stage.id} value={stage.id}>
              {stage.label}
            </option>
          ))}
        </select>
        <select
          aria-label="sort evidence"
          onChange={(event) => setSortMode(event.target.value as "recent" | "quality")}
          value={sortMode}
        >
          <option value="recent">recent</option>
          <option value="quality">quality</option>
        </select>
      </div>

      {filteredRows.length ? (
        <div className="search-evidence-list">
          {filteredRows.map((row) => (
            <article key={row.id}>
              <div className="search-evidence-time">
                <b>{formatRelativeTime(row.receivedAtMs)} ago</b>
                <span>{row.phase}</span>
              </div>
              <div className="search-evidence-copy">
                <div>
                  <b>{row.handle ? `@${row.handle}` : "-"}</b>
                  <span>quality {formatScore(row.quality)}</span>
                  <span>{row.anchor}</span>
                  {row.delta !== null && row.delta !== undefined ? (
                    <span>{formatSignedPercent(row.delta)}</span>
                  ) : null}
                </div>
                <p>{row.text || "No text payload, likely a retweet/reference-only event."}</p>
                <code>{row.id}</code>
              </div>
              {row.url ? (
                <a href={row.url} rel="noreferrer" target="_blank">
                  open
                </a>
              ) : null}
            </article>
          ))}
        </div>
      ) : (
        <PageState.Empty title="当前过滤条件下没有证据行。" />
      )}
    </section>
  );
}

function rowFromPost(post: TokenTimelinePost): EvidenceRow {
  const price = post.price;
  return {
    id: post.event_id,
    receivedAtMs: post.received_at_ms,
    phase: post.stage_phase ?? post.mention_source ?? "post",
    stageId: post.stage_id,
    handle: post.author_handle,
    text: post.text ?? referenceText(post.reference),
    anchor:
      price?.price_usd !== undefined && price?.price_usd !== null
        ? formatTokenPriceUsd(price.price_usd)
        : (price?.status ?? "-"),
    quality: post.post_quality?.score,
    delta: post.price_delta_from_previous_post_pct,
    url: post.url,
  };
}

function rowFromSearchItem(item: SearchItem): EvidenceRow {
  return {
    id: item.event.event_id,
    receivedAtMs: item.event.received_at_ms,
    phase: item.match_type,
    stageId: item.match_type,
    handle: item.event.author_handle,
    text: item.event.text_clean ?? item.event.search_text ?? item.event.content?.text ?? "",
    anchor: "-",
    quality: Math.round(item.score * 1000),
    delta: null,
    url: item.event.canonical_url,
  };
}

function stageOptions(rows: EvidenceRow[]) {
  const seen = new Map<string, string>();
  for (const row of rows) {
    if (row.stageId && !seen.has(row.stageId)) {
      seen.set(row.stageId, row.phase);
    }
  }
  return [...seen.entries()].map(([id, label]) => ({ id, label }));
}

function referenceText(reference?: TokenReference | null) {
  if (!reference) return "";
  const handle = reference.author_handle ? `@${reference.author_handle}` : "unknown author";
  const type = reference.type ?? "reference";
  return `${type} ${handle}${reference.tweet_id ? ` · ${reference.tweet_id}` : ""}`;
}
