import type { OpenApiStatusData } from "@lib/types";
import { IconButton } from "@shared/ui/IconButton";
import { RefreshCw, Search, TriangleAlert } from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";

import "./CockpitTopbar.css";

const NEWS_SEARCH_ARIA_LABEL = "news search";
/*
 * What the box actually searches: the server matches `q` against `search_doc` (context line + leader title)
 * and `leader_title ILIKE`. It does not index venues and does not resolve aliases, so promising
 * `base_symbol / 场所` the way the design does would send readers looking for `hl.perp` into an empty feed
 * (#87 review). Widen the placeholder when the backend widens, not before.
 */
const NEWS_SEARCH_PLACEHOLDER = "搜索新闻事件 / 标题 / 资产";

/**
 * One always-visible number from the pipeline. `value` is undefined until the poll answers.
 *
 * `text` is the escape hatch for a figure that is not a plain count (#88): a hit rate has to arrive with its
 * denominator or not at all, and `56% · N=225` is one server-decided string rather than two numbers the
 * topbar would have to relate to each other.
 */
export type CockpitTopbarFigure = {
  label: string;
  text?: string;
  title?: string;
  tone?: "accent" | "caution";
  value?: number;
};

export type CockpitTopbarProps = {
  figures?: CockpitTopbarFigure[];
  onRefresh: () => void;
  search: {
    onSubmitQuery: (query: string) => void;
    query?: string;
  };
  status: {
    configReady: boolean;
    status?: OpenApiStatusData | null;
    statusError: boolean;
    statusLoading: boolean;
  };
  /** Which surface the reader is on. The frame says where you are; the page says what is on it. */
  title: string;
};

export function CockpitTopbar({
  figures,
  navigationTrigger,
  onRefresh,
  search,
  status,
  title,
}: CockpitTopbarProps & { navigationTrigger?: ReactNode }) {
  const [searchDraft, setSearchDraft] = useState(search.query ?? "");
  const anomaly = healthAnomaly(status);
  useEffect(() => setSearchDraft(search.query ?? ""), [search.query]);
  return (
    <header className="topbar">
      <div className="brand">
        {navigationTrigger}
        <span className="topbar-page-title">{title}</span>
      </div>

      {/* Enter submits. The box is the whole search interaction — there is no hotkey that focuses it. */}
      <form
        className="searchbar"
        onSubmit={(event) => {
          event.preventDefault();
          search.onSubmitQuery(searchDraft);
        }}
      >
        <Search aria-hidden />
        <label className="sr-only" htmlFor="news-search-input">
          {NEWS_SEARCH_ARIA_LABEL}
        </label>
        <input
          aria-label={NEWS_SEARCH_ARIA_LABEL}
          id="news-search-input"
          onChange={(event) => setSearchDraft(event.target.value)}
          placeholder={NEWS_SEARCH_PLACEHOLDER}
          value={searchDraft}
        />
      </form>

      <div className="topbar-right">
        {/*
         * The numbers an operator glances at without opening a page (#87). Hidden until the poll answers
         * rather than shown as zero — a zero that means "not loaded yet" is worse than a gap.
         */}
        {figures?.some((figure) => figure.value != null || figure.text) ? (
          <span className="topbar-figures">
            {figures
              .filter((figure) => figure.value != null || figure.text)
              .map((figure) => (
                <span data-tone={figure.tone} key={figure.label} title={figure.title}>
                  <small>{figure.label}</small>
                  <b>{figure.text ?? new Intl.NumberFormat("zh-CN").format(figure.value ?? 0)}</b>
                </span>
              ))}
          </span>
        ) : null}
        {anomaly ? (
          <span className="topbar-anomaly" role="status" title={anomaly}>
            <TriangleAlert aria-hidden />
            <span>{anomaly}</span>
          </span>
        ) : null}
        <IconButton aria-label="刷新" onClick={onRefresh} title="刷新">
          <RefreshCw aria-hidden />
        </IconButton>
      </div>
    </header>
  );
}

function healthAnomaly({
  configReady,
  status,
  statusError,
  statusLoading,
}: CockpitTopbarProps["status"]): string | null {
  if (!configReady) return "配置未就绪";
  if (statusLoading && !status) return null;
  if (statusError) return "状态检查失败";
  if (status && !status.runtime.ok) return status.runtime.reasons[0] || "服务未就绪";
  return null;
}
