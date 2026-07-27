import { tokenKey } from "@lib/format";
import type { TokenFlowItem, WindowKey } from "@lib/types";
import {
  TOKEN_RADAR_VENUE_FILTERS,
  tokenVenueDisplayLabel,
  type TokenRadarVenueFilter,
} from "@lib/venue";
import { buildTokenRadarCompactCase } from "@shared/model/tokenRadarCompactCase";
import * as PageState from "@shared/ui/PageState";
import { RadarControls } from "@shared/ui/RadarControls";
import { ResearchMark } from "@shared/ui/case-file";
import {
  flexRender,
  getCoreRowModel,
  getSortedRowModel,
  type ColumnDef,
  type SortDirection,
  type SortingState,
  useReactTable,
} from "@tanstack/react-table";
import clsx from "clsx";
import {
  ArrowDown,
  ArrowDownRight,
  ArrowUp,
  ArrowUpRight,
  ChevronsUpDown,
  Minus,
} from "lucide-react";
import { Fragment, useMemo, useState, type KeyboardEvent } from "react";

import type { RadarStatusInput } from "../model/radarContentStatus";
import { tokenRadarDetailHref } from "../model/tokenRadarDetailLink";

import { RadarContentStatus } from "./RadarContentStatus";
import "./TokenRadarTable.css";

type TokenRadarTableProps = {
  items: TokenFlowItem[];
  selectedKey: string | null;
  windowKey: WindowKey;
  isLoading: boolean;
  isRefreshing?: boolean;
  error?: Error | null;
  radarStatus?: RadarStatusInput;
  onSelect?: (item: TokenFlowItem) => void;
  onVenueChange?: (venue: TokenRadarVenueFilter) => void;
  onWindowChange: (window: WindowKey) => void;
  venueFilter?: TokenRadarVenueFilter;
};

export function TokenRadarTable(props: TokenRadarTableProps) {
  const {
    items,
    selectedKey,
    windowKey,
    isLoading,
    isRefreshing = false,
    error,
    radarStatus,
    onSelect,
    onVenueChange = () => undefined,
    onWindowChange,
    venueFilter = "all",
  } = props;
  const showLoading = !error && isLoading && items.length === 0;
  const showEmpty = !error && !showLoading && items.length === 0;
  const resultLabel = showLoading
    ? "loading"
    : items.length
      ? `${items.length} live ${items.length === 1 ? "case" : "cases"}${
          isRefreshing ? " · updating" : ""
        }`
      : "no live cases";
  const columns = useMemo<ColumnDef<TokenFlowItem>[]>(
    () => tokenRadarColumns({ onSelect, selectedKey }),
    [onSelect, selectedKey],
  );
  const [sorting, setSorting] = useState<SortingState>([]);
  const table = useReactTable({
    data: items,
    columns,
    getCoreRowModel: getCoreRowModel(),
    getRowId: (item, index) =>
      `${tokenKey(item)}:${item.radar?.computed_at_ms ?? item.flow.window_end_ms ?? "row"}:${index}`,
    getSortedRowModel: getSortedRowModel(),
    onSortingChange: setSorting,
    state: { sorting },
  });

  return (
    <section className="radar-panel" aria-label="Token Radar">
      <header className="radar-toolbar">
        <div className="radar-toolbar-primary">
          <div className="radar-scan-title">
            <h2>Token Radar</h2>
            <span>{resultLabel}</span>
          </div>
          {radarStatus ? <RadarContentStatus status={radarStatus} /> : null}
        </div>
        <div className="toolbar-controls" aria-label="token radar scan controls">
          <VenueFilter value={venueFilter} onChange={onVenueChange} />
          <RadarControls windowKey={windowKey} onWindowChange={onWindowChange} />
        </div>
      </header>

      <div className="token-radar-table">
        {showLoading ? (
          <PageState.Loading layout="panel" rows={8} label="loading token radar" />
        ) : null}
        {error ? <PageState.Error error={`Token Radar 暂不可用 · ${error.message}`} /> : null}
        {showEmpty ? <PageState.Empty title="当前窗口暂无可交易 token 热度" /> : null}
        {!showLoading && !error && items.length ? (
          <PageState.Stale updating={isRefreshing}>
            <div className="radar-data-table">
              <div>
                {table.getHeaderGroups().map((headerGroup) => (
                  <div className="token-radar-head" key={headerGroup.id}>
                    {headerGroup.headers.map((header) => (
                      <div
                        className={`radar-head-cell ${header.column.id}`}
                        data-sort={sortState(header.column.getIsSorted())}
                        key={header.id}
                      >
                        <button
                          aria-label={`Sort by ${headerLabel(header.column.id).toLowerCase()}`}
                          className="radar-sort-button"
                          type="button"
                          onClick={header.column.getToggleSortingHandler()}
                        >
                          <span>
                            {flexRender(header.column.columnDef.header, header.getContext())}
                          </span>
                          <SortIcon direction={header.column.getIsSorted()} />
                        </button>
                      </div>
                    ))}
                  </div>
                ))}
              </div>
              <div>
                {table.getRowModel().rows.map((row) => {
                  const key = tokenKey(row.original);
                  const tokenCase = buildTokenRadarCompactCase(row.original);
                  return (
                    <Fragment key={row.id}>
                      {/* eslint-disable jsx-a11y/no-noninteractive-element-interactions, jsx-a11y/no-noninteractive-tabindex */}
                      <article
                        aria-label={`Token Radar item ${tokenCase.label}`}
                        className={clsx("token-radar-row", selectedKey === key && "selected")}
                        tabIndex={0}
                        onClick={() => onSelect?.(row.original)}
                        onKeyDown={(event) => handleRowKeyDown(event, row.original, onSelect)}
                      >
                        {row.getVisibleCells().map((cell) => (
                          <div className={`token-radar-cell ${cell.column.id}`} key={cell.id}>
                            {flexRender(cell.column.columnDef.cell, cell.getContext())}
                          </div>
                        ))}
                      </article>
                      {/* eslint-enable jsx-a11y/no-noninteractive-element-interactions, jsx-a11y/no-noninteractive-tabindex */}
                    </Fragment>
                  );
                })}
              </div>
            </div>
          </PageState.Stale>
        ) : null}
      </div>
    </section>
  );
}

type TokenRadarColumnDeps = {
  onSelect: TokenRadarTableProps["onSelect"];
  selectedKey: string | null;
};

function VenueFilter({
  onChange,
  value,
}: {
  onChange: (value: TokenRadarVenueFilter) => void;
  value: TokenRadarVenueFilter;
}) {
  return (
    <div className="token-radar-venue-filter" aria-label="token radar venue filter">
      {TOKEN_RADAR_VENUE_FILTERS.map((item) => (
        <button
          aria-pressed={item.key === value}
          className={item.key === value ? "active" : ""}
          key={item.key}
          type="button"
          onClick={() => onChange(item.key)}
        >
          {item.label}
        </button>
      ))}
    </div>
  );
}

function tokenRadarColumns({
  onSelect,
  selectedKey,
}: TokenRadarColumnDeps): ColumnDef<TokenFlowItem>[] {
  return [
    {
      id: "case",
      header: "Token case",
      accessorFn: (item) => item.identity.symbol ?? tokenKey(item),
      cell: ({ row }) => (
        <TokenCaseCell
          detailHref={tokenRadarDetailHref(row.original)}
          item={row.original}
          onSelect={onSelect}
          selected={selectedKey === tokenKey(row.original)}
        />
      ),
    },
    {
      id: "social",
      header: "Social",
      accessorFn: (item) => item.flow.mentions,
      cell: ({ row }) => <SocialCell item={row.original} />,
    },
    {
      id: "token-radar-propagation",
      header: "Propagation",
      accessorFn: (item) => item.propagation.score,
      cell: ({ row }) => <PropagationCell item={row.original} />,
    },
    {
      id: "market",
      header: "Market",
      accessorFn: (item) =>
        item.market.market_cap ??
        item.market.price ??
        item.market.liquidity ??
        item.market.volume_24h ??
        item.market.holder_count ??
        -1,
      cell: ({ row }) => <MarketCell item={row.original} />,
    },
    {
      id: "score",
      header: "Score",
      accessorFn: (item) => item.opportunity.score,
      cell: ({ row }) => <ScoreCell item={row.original} />,
    },
    {
      id: "listed",
      header: "Listed",
      accessorFn: (item) =>
        item.radar?.listed_at_ms ?? item.radar?.computed_at_ms ?? item.flow.window_end_ms ?? 0,
      cell: ({ row }) => <ListedCell item={row.original} />,
    },
  ];
}

function handleRowKeyDown(
  event: KeyboardEvent<HTMLElement>,
  item: TokenFlowItem,
  onSelect: TokenRadarTableProps["onSelect"],
) {
  if (event.key !== "Enter" && event.key !== " ") {
    return;
  }
  event.preventDefault();
  onSelect?.(item);
}

function TokenCaseCell({
  detailHref,
  item,
  onSelect,
  selected,
}: {
  detailHref: string;
  item: TokenFlowItem;
  onSelect: TokenRadarTableProps["onSelect"];
  selected: boolean;
}) {
  const tokenCase = buildTokenRadarCompactCase(item);
  const venueLabel = tokenVenueDisplayLabel(item);
  return (
    <div className="radar-case-cell" data-case-section="identity">
      {tokenCase.logoUrl ? (
        <img alt="" className="radar-token-logo" src={tokenCase.logoUrl} />
      ) : (
        <ResearchMark
          className="radar-case-mark"
          label={tokenCase.label}
          tone={tokenCase.markTone}
        />
      )}
      <span className="radar-case-copy">
        <span className="radar-case-symbol-row">
          <a
            aria-label={`Open token item ${tokenCase.label}`}
            className={clsx("radar-case-button", selected && "selected")}
            href={detailHref}
            onClick={(event) => {
              event.preventDefault();
              event.stopPropagation();
              onSelect?.(item);
            }}
          >
            <strong>{tokenCase.label}</strong>
          </a>
          {venueLabel ? (
            <span className="radar-venue-badge" title={tokenCase.subtitle}>
              {venueLabel}
            </span>
          ) : null}
          {tokenCase.externalLinks.length ? (
            <nav className="radar-case-links" aria-label={`External links for ${tokenCase.label}`}>
              {tokenCase.externalLinks.map((link) => (
                <a
                  className={clsx("radar-case-link", link.tone)}
                  href={link.href}
                  key={`${link.label}:${link.href}`}
                  rel="noreferrer"
                  target="_blank"
                  onClick={(event) => event.stopPropagation()}
                >
                  {link.label}
                </a>
              ))}
            </nav>
          ) : null}
        </span>
        <span className="radar-case-meta">{tokenCase.subtitle}</span>
      </span>
    </div>
  );
}

function SocialCell({ item }: { item: TokenFlowItem }) {
  const tokenCase = buildTokenRadarCompactCase(item);
  return (
    <span className="radar-fact social-fact" data-case-section="social">
      <b>{tokenCase.socialFact}</b>
      <em>{tokenCase.socialDetail}</em>
    </span>
  );
}

function PropagationCell({ item }: { item: TokenFlowItem }) {
  const tokenCase = buildTokenRadarCompactCase(item);
  return (
    <span className="radar-fact token-radar-propagation-fact" data-case-section="propagation">
      <b>{tokenCase.propagation.value}</b>
      <em>{tokenCase.propagation.detail}</em>
    </span>
  );
}

function MarketCell({ item }: { item: TokenFlowItem }) {
  const tokenCase = buildTokenRadarCompactCase(item);
  return (
    <span className="radar-fact market-fact" data-radar-metric="market">
      <span className="market-primary-line">
        <b className="market-primary-value">{tokenCase.market.value}</b>
        <span className={clsx("market-move", tokenCase.marketMove.direction)}>
          {tokenCase.marketMove.direction === "up" ? <ArrowUpRight aria-hidden /> : null}
          {tokenCase.marketMove.direction === "down" ? <ArrowDownRight aria-hidden /> : null}
          {tokenCase.marketMove.direction === "flat" ? <Minus aria-hidden /> : null}
          <b>{tokenCase.marketMove.value}</b>
        </span>
      </span>
      <span className="market-stats" aria-label={tokenCase.market.detail}>
        {tokenCase.market.stats.length ? (
          tokenCase.market.stats.map((stat) => (
            <span className={clsx("market-stat", stat.tone)} key={stat.key} title={stat.status}>
              <span>{stat.label}</span>
              <b>{stat.value}</b>
            </span>
          ))
        ) : (
          <em>market data unavailable</em>
        )}
      </span>
    </span>
  );
}

function ScoreCell({ item }: { item: TokenFlowItem }) {
  const tokenCase = buildTokenRadarCompactCase(item);
  return (
    <span className="radar-score-cell" data-case-section="score">
      <span className="radar-score">{tokenCase.score}</span>
    </span>
  );
}

function ListedCell({ item }: { item: TokenFlowItem }) {
  const tokenCase = buildTokenRadarCompactCase(item);
  return (
    <span className="radar-listed-action-cell" data-case-section="action">
      <span className="radar-fact listed-fact" data-radar-metric="listed">
        <b>{tokenCase.listed.value}</b>
        <em>{tokenCase.listed.detail}</em>
      </span>
    </span>
  );
}

function headerLabel(columnId: string): string {
  const labels: Record<string, string> = {
    case: "token case",
    social: "social",
    "token-radar-propagation": "propagation",
    market: "market",
    listed: "listed",
    score: "score",
  };
  return labels[columnId] ?? columnId;
}

function sortState(direction: false | SortDirection): "ascending" | "descending" | "none" {
  if (direction === "asc") {
    return "ascending";
  }
  if (direction === "desc") {
    return "descending";
  }
  return "none";
}

function SortIcon({ direction }: { direction: false | SortDirection }) {
  if (direction === "asc") {
    return <ArrowUp aria-hidden />;
  }
  if (direction === "desc") {
    return <ArrowDown aria-hidden />;
  }
  return <ChevronsUpDown aria-hidden />;
}
