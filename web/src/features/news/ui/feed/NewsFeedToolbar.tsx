import { ChevronDown, SlidersHorizontal } from "lucide-react";
import { useState } from "react";

import {
  NEWS_FEED_CHANNELS,
  NEWS_FEED_DIRECTIONS,
  NEWS_FEED_HOURS,
  type NewsFeedChannel,
  type NewsFeedCounts,
  type NewsFeedDirection,
  type NewsFeedFilters,
  type NewsFeedOutcome,
} from "../../api/newsQueries";
import { type FeedFilterChanges, toggleFilterValue } from "../../model/feedFilters";
import { formatCount, hoursLabel, outcomeTabLabel } from "../../model/newsLabels";

import "./newsFeedToolbar.css";

const OUTCOME_TABS: Array<NewsFeedOutcome | null> = ["pushed", "held", "pending", null];
const DIRECTION_LABELS: Record<NewsFeedDirection, string> = {
  bullish: "▲ 利多",
  bearish: "▼ 利空",
  neutral: "◆ 中性",
};
const CHANNEL_LABELS: Record<NewsFeedChannel, string> = { news: "新闻", oi: "OI 帧" };

/** The Event-feed controls in the approved order: task, count, window, then two bounded filter axes. */
export function NewsFeedToolbar({
  counts,
  filters,
  onChange,
  visibleCount,
}: {
  counts?: NewsFeedCounts;
  filters: NewsFeedFilters;
  onChange: (changes: FeedFilterChanges) => void;
  visibleCount: number;
}) {
  const total = tabCount(counts, filters.outcome);
  const [timeOpen, setTimeOpen] = useState(false);
  const [filtersOpen, setFiltersOpen] = useState(false);
  const activeFilters = filters.directions.length + filters.channels.length;
  return (
    <>
      <div className="news-feed-toolbar">
        <OutcomeTabs
          active={filters.outcome}
          counts={counts}
          onChange={(outcome) => onChange({ outcome })}
        />
        <div className="news-filter-bar">
          {total == null ? null : (
            <span className="news-filter-total">
              {formatCount(visibleCount)} / {formatCount(total)} 条
            </span>
          )}
          <TimeMenu
            hours={filters.hours}
            onChange={(hours) => onChange({ hours })}
            onOpenChange={(open) => {
              setTimeOpen(open);
              if (open) setFiltersOpen(false);
            }}
            open={timeOpen}
          />
          <button
            aria-expanded={filtersOpen}
            className="news-filter-trigger"
            data-active={activeFilters > 0 || undefined}
            onClick={() => {
              setFiltersOpen((open) => !open);
              setTimeOpen(false);
            }}
            type="button"
          >
            <SlidersHorizontal aria-hidden />
            筛选
            {activeFilters ? <span aria-hidden>{activeFilters}</span> : null}
          </button>
        </div>
      </div>
      {filtersOpen ? <FilterPanel filters={filters} onChange={onChange} /> : null}
    </>
  );
}

function OutcomeTabs({
  active,
  counts,
  onChange,
}: {
  active: NewsFeedOutcome | null;
  counts?: NewsFeedCounts;
  onChange: (value: NewsFeedOutcome | null) => void;
}) {
  return (
    <div aria-label="按结局筛选" className="news-segmented" role="tablist">
      {OUTCOME_TABS.map((value) => {
        const count = tabCount(counts, value);
        return (
          <button
            aria-label={
              count == null
                ? outcomeTabLabel(value)
                : `${outcomeTabLabel(value)} ${compactTabCount(count)}`
            }
            aria-selected={active === value}
            className="news-segmented-option"
            data-active={active === value || undefined}
            key={value ?? "all"}
            onClick={() => onChange(value)}
            role="tab"
            type="button"
          >
            {outcomeTabLabel(value)}
            {count == null ? null : (
              <span className="news-segmented-count">{compactTabCount(count)}</span>
            )}
          </button>
        );
      })}
    </div>
  );
}

function compactTabCount(value: number): string {
  if (value < 1_000) return String(value);
  return `${Math.floor(value / 100) / 10}k`;
}

function TimeMenu({
  hours,
  onChange,
  onOpenChange,
  open,
}: {
  hours: number | null;
  onChange: (hours: number) => void;
  onOpenChange: (open: boolean) => void;
  open: boolean;
}) {
  return (
    <div className="news-menu">
      <button aria-expanded={open} onClick={() => onOpenChange(!open)} type="button">
        {hoursLabel(hours)}
        <ChevronDown aria-hidden />
      </button>
      {open ? (
        <div aria-label="时间范围" className="news-menu-popover" role="menu">
          {NEWS_FEED_HOURS.map((value) => (
            <button
              aria-checked={hours === value}
              key={value}
              onClick={() => {
                onChange(value);
                onOpenChange(false);
              }}
              role="menuitemradio"
              type="button"
            >
              {hoursLabel(value)}
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function FilterPanel({
  filters,
  onChange,
}: {
  filters: NewsFeedFilters;
  onChange: (changes: FeedFilterChanges) => void;
}) {
  return (
    <div className="news-filter-panel">
      <small>方向</small>
      <div>
        {NEWS_FEED_DIRECTIONS.map((value) => (
          <button
            aria-pressed={filters.directions.includes(value)}
            data-direction={value}
            key={value}
            onClick={() =>
              onChange({
                directions: toggleFilterValue(filters.directions, value, NEWS_FEED_DIRECTIONS),
              })
            }
            type="button"
          >
            {DIRECTION_LABELS[value]}
          </button>
        ))}
      </div>
      <small>通道</small>
      <div>
        {NEWS_FEED_CHANNELS.map((value) => (
          <button
            aria-pressed={filters.channels.includes(value)}
            key={value}
            onClick={() =>
              onChange({
                channels: toggleFilterValue(filters.channels, value, NEWS_FEED_CHANNELS),
              })
            }
            type="button"
          >
            {CHANNEL_LABELS[value]}
          </button>
        ))}
      </div>
    </div>
  );
}

function tabCount(
  counts: NewsFeedCounts | undefined,
  value: NewsFeedOutcome | null,
): number | null {
  if (!counts) return null;
  return value == null ? counts.total : counts[value];
}
