import type { AppSession } from "@app/useAppSession";
import {
  useCockpitStatusQuery,
  type CockpitShellProps,
  type SearchShellProps,
} from "@features/cockpit";
import { useLiveRouteState } from "@features/live/shell";
import type { WindowKey } from "@lib/types";
import { searchPath } from "@shared/routing/paths";
import { searchWithOptionalPrefix } from "@shared/routing/searchParams";
import { useSocketSnapshot } from "@shared/socket/socketContext";
import { useQueryClient } from "@tanstack/react-query";
import { useRef } from "react";
import { useLocation, useNavigate } from "react-router-dom";

export type ShellRouteContext = {
  token: string;
  updateWindow: (window: WindowKey) => void;
  windowKey: WindowKey;
};

export type ShellChromeData = {
  cockpitShellProps: CockpitShellProps;
  routeContext: ShellRouteContext;
  searchShellProps: SearchShellProps;
};

export function useShellChromeData(session: AppSession): ShellChromeData {
  const location = useLocation();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const liveRoute = useLiveRouteState();
  const statusQuery = useCockpitStatusQuery({ token: session.token });
  const socketSnapshot = useSocketSnapshot();
  const searchInputRef = useRef<HTMLInputElement | null>(null);
  const status = statusQuery.data?.data ?? null;
  const token = session.token;
  const windowKey = liveRoute.window;
  const routeContext: ShellRouteContext = {
    token,
    updateWindow: liveRoute.updateWindow,
    windowKey,
  };
  const handleHotkey = (event: KeyboardEvent) => {
    const target = event.target as HTMLElement;
    const isTyping = target.tagName === "INPUT" || target.tagName === "TEXTAREA";
    if (event.key === "/" && !isTyping) {
      event.preventDefault();
      searchInputRef.current?.focus();
      return;
    }
    if (isTyping) {
      return;
    }
    if (!shouldHandleLiveWindowHotkey(location.pathname, event.key)) {
      return;
    }
    if (event.key === "1") liveRoute.updateWindow("5m");
    if (event.key === "2") liveRoute.updateWindow("1h");
    if (event.key === "3") liveRoute.updateWindow("4h");
    if (event.key === "4") liveRoute.updateWindow("24h");
  };
  const searchTargetsNews = shouldRouteTopbarSearchToNews(location.pathname);
  const submitTopbarSearch = (searchText: string) => {
    if (!searchTargetsNews) {
      navigate(searchPath({ q: searchText.trim(), window: "24h" }));
      return;
    }
    const query = searchText.trim();
    const next = new URLSearchParams(location.search);
    if (query) {
      next.set("q", query);
    } else {
      next.delete("q");
    }
    navigate({
      pathname: "/news",
      search: searchWithOptionalPrefix(next),
    });
  };
  const topbarProps = {
    search: {
      ariaLabel: searchTargetsNews ? "news search" : "global search",
      inputRef: searchInputRef,
      onSubmitQuery: submitTopbarSearch,
      placeholder: searchTargetsNews ? "搜索新闻 / 来源 / token" : "搜索 token / @handle / CA",
    },
    status: {
      socketStatus: socketSnapshot.status,
      lastSocketMessageAt: socketSnapshot.lastMessageAt,
      status,
      statusLoading: Boolean(token) && statusQuery.isPending,
      statusError: statusQuery.isError,
      configReady: Boolean(token),
    },
    onRefresh: () => void queryClient.invalidateQueries(),
  };
  const shellProps = {
    topbar: topbarProps,
    onHotkey: handleHotkey,
    outletContext: routeContext,
  };

  return {
    cockpitShellProps: shellProps,
    routeContext,
    searchShellProps: {
      ...shellProps,
      topbar: {
        ...shellProps.topbar,
        search: { ...shellProps.topbar.search, showMainRouteButton: true },
      },
    },
  };
}

export function shouldRouteTopbarSearchToNews(pathname: string): boolean {
  const path = pathname.split("?")[0] ?? pathname;
  return path === "/news" || path.startsWith("/news/");
}

export function shouldHandleLiveWindowHotkey(pathname: string, key: string): boolean {
  if (!["1", "2", "3", "4"].includes(key)) {
    return false;
  }
  const path = pathname.split("?")[0] ?? pathname;
  return path === "/" || path.startsWith("/stocks");
}
