import type { AppSession } from "@app/useAppSession";
import { useCockpitStatusQuery, type CockpitShellProps } from "@features/cockpit";
import { newsPath } from "@shared/routing/paths";
import { searchWithOptionalPrefix } from "@shared/routing/searchParams";
import { useQueryClient } from "@tanstack/react-query";
import { useRef } from "react";
import { useLocation, useNavigate } from "react-router-dom";

export type ShellRouteContext = {
  bootstrapError: boolean;
  bootstrapLoading: boolean;
  token: string;
};

export type ShellChromeData = {
  cockpitShellProps: CockpitShellProps;
  routeContext: ShellRouteContext;
};

export function useShellChromeData(session: AppSession): ShellChromeData {
  const location = useLocation();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const statusQuery = useCockpitStatusQuery({ token: session.token });
  const searchInputRef = useRef<HTMLInputElement | null>(null);
  const status = statusQuery.data?.data ?? null;
  const token = session.token;
  const routeContext: ShellRouteContext = {
    bootstrapError: session.bootstrapError,
    bootstrapLoading: session.bootstrapLoading,
    token,
  };
  const handleHotkey = (event: KeyboardEvent) => {
    const target = event.target as HTMLElement;
    const isTyping = target.tagName === "INPUT" || target.tagName === "TEXTAREA";
    if (event.key === "/" && !isTyping) {
      event.preventDefault();
      searchInputRef.current?.focus();
      return;
    }
  };
  const currentSearchQuery = isNewsRoute(location.pathname)
    ? (new URLSearchParams(location.search).get("q") ?? "")
    : "";
  const submitTopbarSearch = (searchText: string) => {
    const query = searchText.trim();
    const next = isNewsRoute(location.pathname)
      ? new URLSearchParams(location.search)
      : new URLSearchParams();
    if (query) {
      next.set("q", query);
    } else {
      next.delete("q");
    }
    navigate({
      pathname: newsPath(),
      search: searchWithOptionalPrefix(next),
    });
  };

  return {
    cockpitShellProps: {
      topbar: {
        search: {
          inputRef: searchInputRef,
          onSubmitQuery: submitTopbarSearch,
          query: currentSearchQuery,
        },
        status: {
          status,
          statusLoading: Boolean(token) && statusQuery.isPending,
          statusError: statusQuery.isError,
          configReady: Boolean(token),
        },
        onRefresh: () => void queryClient.invalidateQueries(),
      },
      onHotkey: handleHotkey,
      outletContext: routeContext,
    },
    routeContext,
  };
}

function isNewsRoute(pathname: string): boolean {
  return pathname === "/news" || pathname.startsWith("/news/");
}
