import type { OpenApiStatusData } from "@lib/types";
import { IconButton } from "@shared/ui/IconButton";
import { RefreshCw, Search, TriangleAlert } from "lucide-react";
import { useEffect, useState, type ReactNode, type RefObject } from "react";

import "./CockpitTopbar.css";

const NEWS_SEARCH_ARIA_LABEL = "news search";
const NEWS_SEARCH_PLACEHOLDER = "搜索新闻事件 / 标题 / 资产";

export type CockpitTopbarProps = {
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
