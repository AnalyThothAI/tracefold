import type { OpenApiStatusData } from "@lib/types";
import { IconButton } from "@shared/ui/IconButton";
import { RefreshCw, Search, TriangleAlert } from "lucide-react";
import { useEffect, useState, type ReactNode, type RefObject } from "react";

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
  tone?: "caution";
  value?: number;
};

export type CockpitTopbarProps = {
  figures?: CockpitTopbarFigure[];
  navigationTrigger?: ReactNode;
  search: {
    inputRef: RefObject<HTMLInputElement | null>;
    onSubmitQuery: (query: string) => void;
    query?: string;
  };
  status: {
    status?: OpenApiStatusData | null;
    statusLoading: boolean;
    statusError: boolean;
    configReady: boolean;
  };
  onRefresh: () => void;
};

export function CockpitTopbar({
  figures,
  navigationTrigger,
  search,
  status,
  onRefresh,
}: CockpitTopbarProps) {
  const [searchDraft, setSearchDraft] = useState(search.query ?? "");
  const anomaly = healthAnomaly(status);
  useEffect(() => setSearchDraft(search.query ?? ""), [search.query]);
  return (
    <header className="topbar">
      <div className="brand">
        {navigationTrigger ? (
          <span className="topbar-sidebar-trigger-slot">{navigationTrigger}</span>
        ) : null}
        <span className="topbar-product-name">Tracefold</span>
      </div>

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
          placeholder={NEWS_SEARCH_PLACEHOLDER}
          ref={search.inputRef}
          value={searchDraft}
          onChange={(event) => setSearchDraft(event.target.value)}
        />
        <button type="submit">检索</button>
      </form>

      {/*
       * The two figures an operator glances at without opening a page (#87). They are hidden until the poll
       * answers rather than shown as zero — a zero that means "not loaded yet" is worse than a gap.
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
      <IconButton
        aria-label="刷新"
        className="topbar-refresh-button"
        title="刷新"
        onClick={onRefresh}
      >
        <RefreshCw aria-hidden />
      </IconButton>
    </header>
  );
}

function healthAnomaly({
  configReady,
  status,
  statusLoading,
  statusError,
}: CockpitTopbarProps["status"]): string | null {
  if (!configReady) {
    return "配置未就绪";
  }
  if (statusLoading && !status) {
    return null;
  }
  if (statusError) {
    return "状态检查失败";
  }
  if (status && !status.runtime.ok) {
    return status.runtime.reasons[0] || "服务未就绪";
  }
  return null;
}
