import type { OpenApiStatusData } from "@lib/types";
import { IconButton } from "@shared/ui/IconButton";
import { Home, RefreshCw, Search, TriangleAlert } from "lucide-react";
import { useEffect, useState, type ReactNode, type RefObject } from "react";
import { useNavigate } from "react-router-dom";

import "./CockpitTopbar.css";

export type CockpitTopbarProps = {
  navigationTrigger?: ReactNode;
  search: {
    ariaLabel?: string;
    inputRef: RefObject<HTMLInputElement | null>;
    onSubmitQuery: (query: string) => void;
    placeholder?: string;
    query?: string;
    showMainRouteButton?: boolean;
  };
  status: {
    socketStatus: string;
    lastSocketMessageAt: number | null;
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
  const navigate = useNavigate();
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
        {search.showMainRouteButton ? (
          <button className="main-route-button" type="button" onClick={() => navigate("/")}>
            <Home aria-hidden />
            Main
          </button>
        ) : null}
      </div>

      <form
        className="searchbar"
        onSubmit={(event) => {
          event.preventDefault();
          search.onSubmitQuery(searchDraft);
        }}
      >
        <Search aria-hidden />
        <label className="sr-only" htmlFor="global-search-input">
          {search.ariaLabel ?? "global search"}
        </label>
        <input
          aria-label={search.ariaLabel ?? "global search"}
          id="global-search-input"
          placeholder={search.placeholder ?? "搜索 token / @handle / CA"}
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
  socketStatus,
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
  if (status && status.providers.status !== "ok") {
    return status.providers.reasons[0] || "Provider 状态不可用";
  }
  if (socketStatus !== "connected") {
    return `实时连接 ${socketStatus}`;
  }
  return null;
}
