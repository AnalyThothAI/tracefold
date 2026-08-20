import { SlidersHorizontal, X } from "lucide-react";
import { useEffect, useState } from "react";

import {
  NEWS_FEED_DECISIONS,
  NEWS_FEED_HOURS,
  NEWS_FEED_OUTCOMES,
  NEWS_FEED_PRIORITIES,
  type NewsFeedCounts,
  type NewsFeedFilters,
  type NewsFeedOutcome,
} from "../../api/newsQueries";
import {
  ADMISSION_FILTER_LABELS,
  DECISION_FILTER_LABELS,
  FAMILY_FILTER_LABELS,
  KNOWN_ADMISSIONS,
  KNOWN_FAMILIES,
  PRIORITY_FILTER_LABELS,
  type FeedFilterChanges,
  normalizeSymbol,
  parseDecision,
  parseHours,
  parsePriority,
  parseSort,
  withSelectedOption,
} from "../../model/feedFilters";
import { formatCount, hoursLabel, outcomeTabLabel } from "../../model/newsLabels";
import { NewsToneDot } from "../chrome/NewsTone";

import "./newsFeedToolbar.css";

const OUTCOME_TABS: Array<NewsFeedOutcome | null> = [null, ...NEWS_FEED_OUTCOMES];
const OUTCOME_TAB_TONE = { pushed: "done", held: "neutral", pending: "info" } as const;

export function NewsFeedToolbar({
  counts,
  filters,
  hasAdvanced,
  onChange,
  visibleCount,
}: {
  counts?: NewsFeedCounts;
  filters: NewsFeedFilters;
  hasAdvanced: boolean;
  onChange: (changes: FeedFilterChanges) => void;
  visibleCount: number;
}) {
  return (
    <div className="news-feed-toolbar">
      <div className="news-feed-toolbar-left">
        <OutcomeTabs
          active={filters.outcome}
          counts={counts}
          onChange={(outcome) => onChange({ outcome })}
        />
      </div>
      <FeedControls
        counts={counts}
        filters={filters}
        hasAdvanced={hasAdvanced}
        onChange={onChange}
        visibleCount={visibleCount}
      />
    </div>
  );
}

/**
 * The four task tabs. Each carries the server's count for that group under the *current* filter and window, so
 * the reader can see a tab is empty without visiting it. Digits 1–4 select them; the hint is desktop-only.
 */
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
      {OUTCOME_TABS.map((value, index) => {
        const count = tabCount(counts, value);
        return (
          <button
            aria-selected={active === value}
            className="news-segmented-option news-toned"
            data-outcome-group={value ?? "all"}
            data-tone={value ? OUTCOME_TAB_TONE[value] : "neutral"}
            key={value ?? "all"}
            onClick={() => onChange(value)}
            role="tab"
            type="button"
          >
            <NewsToneDot halo={false} />
            {outcomeTabLabel(value)}
            {/*
             * The count and the digit hint are decoration on the tab, not part of what it selects. Folding a
             * number that changes every three seconds into the accessible name would make the tab rename
             * itself constantly; the labelled 24 h funnel above announces the same figures properly.
             */}
            {count == null ? null : (
              <span aria-hidden className="news-segmented-count">
                {formatCount(count)}
              </span>
            )}
            <kbd aria-hidden className="news-segmented-key">
              {index + 1}
            </kbd>
          </button>
        );
      })}
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

function FeedControls({
  counts,
  filters,
  hasAdvanced,
  onChange,
  visibleCount,
}: {
  counts?: NewsFeedCounts;
  filters: NewsFeedFilters;
  hasAdvanced: boolean;
  onChange: (changes: FeedFilterChanges) => void;
  visibleCount: number;
}) {
  const [symbolDraft, setSymbolDraft] = useState(filters.symbol ?? "");
  useEffect(() => {
    setSymbolDraft(filters.symbol ?? "");
  }, [filters.symbol]);
  const total = tabCount(counts, filters.outcome);
  return (
    <div className="news-filter-bar">
      {total == null ? null : (
        <span className="news-filter-total">
          {formatCount(visibleCount)} / {formatCount(total)}
        </span>
      )}
      <label className="news-select">
        <span className="sr-only">时间范围</span>
        <select
          aria-label="时间范围"
          onChange={(event) => onChange({ hours: parseHours(event.target.value) })}
          value={filters.hours == null ? "all" : String(filters.hours)}
        >
          {NEWS_FEED_HOURS.map((hours) => (
            <option key={hours} value={String(hours)}>
              {hoursLabel(hours)}
            </option>
          ))}
          <option value="all">全部时间</option>
        </select>
      </label>
      <label className="news-select">
        <span className="sr-only">排序</span>
        <select
          aria-label="事件排序"
          onChange={(event) => onChange({ sort: parseSort(event.target.value) })}
          value={filters.sort}
        >
          <option value="latest">最新在前</option>
          <option value="priority">高优先级在前</option>
        </select>
      </label>
      <details className="news-filter-disclosure">
        <summary data-active={hasAdvanced || undefined}>
          <SlidersHorizontal aria-hidden />
          筛选
        </summary>
        <div>
          <label>
            <span>来源类别</span>
            <select
              aria-label="事件家族"
              onChange={(event) => onChange({ family: event.target.value || null })}
              value={filters.family ?? ""}
            >
              <option value="">全部</option>
              {withSelectedOption(KNOWN_FAMILIES, filters.family).map((value) => (
                <option key={value} value={value}>
                  {FAMILY_FILTER_LABELS[value] ?? value}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span>门禁</span>
            <select
              aria-label="事件准入"
              onChange={(event) => onChange({ admission: event.target.value || null })}
              value={filters.admission ?? ""}
            >
              <option value="">全部</option>
              {withSelectedOption(KNOWN_ADMISSIONS, filters.admission).map((value) => (
                <option key={value} value={value}>
                  {ADMISSION_FILTER_LABELS[value] ?? value}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span>优先级</span>
            <select
              aria-label="事件优先级"
              onChange={(event) => onChange({ priority: parsePriority(event.target.value) })}
              value={filters.priority ?? ""}
            >
              <option value="">全部</option>
              {NEWS_FEED_PRIORITIES.map((value) => (
                <option key={value} value={value}>
                  {PRIORITY_FILTER_LABELS[value]}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span>决策</span>
            <select
              aria-label="Triage 判定"
              onChange={(event) => onChange({ decision: parseDecision(event.target.value) })}
              value={filters.decision ?? ""}
            >
              <option value="">全部</option>
              {NEWS_FEED_DECISIONS.map((value) => (
                <option key={value} value={value}>
                  {DECISION_FILTER_LABELS[value]}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span>资产代码</span>
            <input
              aria-label="落地资产"
              autoCapitalize="characters"
              maxLength={16}
              onBlur={() => onChange({ symbol: normalizeSymbol(symbolDraft) })}
              onChange={(event) => setSymbolDraft(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") {
                  event.preventDefault();
                  onChange({ symbol: normalizeSymbol(symbolDraft) });
                }
              }}
              placeholder="BTC"
              value={symbolDraft}
            />
          </label>
        </div>
      </details>
    </div>
  );
}

export function NewsActiveFilterChips({
  filters,
  onRemove,
}: {
  filters: NewsFeedFilters;
  onRemove: (changes: FeedFilterChanges) => void;
}) {
  const chips = [
    filters.q ? { label: `搜索：${filters.q}`, remove: () => onRemove({ q: null }) } : null,
    filters.family
      ? {
          label: `来源类别：${FAMILY_FILTER_LABELS[filters.family] ?? filters.family}`,
          remove: () => onRemove({ family: null }),
        }
      : null,
    filters.admission
      ? {
          label: `门禁：${ADMISSION_FILTER_LABELS[filters.admission] ?? filters.admission}`,
          remove: () => onRemove({ admission: null }),
        }
      : null,
    filters.priority
      ? {
          label: `优先级：${PRIORITY_FILTER_LABELS[filters.priority]}`,
          remove: () => onRemove({ priority: null }),
        }
      : null,
    filters.decision
      ? {
          label: `决策：${DECISION_FILTER_LABELS[filters.decision]}`,
          remove: () => onRemove({ decision: null }),
        }
      : null,
    filters.symbol
      ? { label: `资产：${filters.symbol}`, remove: () => onRemove({ symbol: null }) }
      : null,
  ].filter((chip): chip is { label: string; remove: () => void } => chip !== null);
  if (!chips.length) return null;
  return (
    <div aria-label="已启用筛选" className="news-active-filters" role="group">
      {chips.map((chip) => (
        <button
          aria-label={`移除${chip.label}`}
          key={chip.label}
          onClick={chip.remove}
          type="button"
        >
          {chip.label}
          <X aria-hidden />
        </button>
      ))}
    </div>
  );
}
