import type { AppSession } from "@app/useAppSession";
import { APP_SHORTCUTS, useCockpitStatusQuery, type CockpitShellProps } from "@features/cockpit";
import { useNewsStatusWithToken } from "@features/news/shell";
import { newsPath, newsStatusPath } from "@shared/routing/paths";
import { searchWithOptionalPrefix } from "@shared/routing/searchParams";
import { useQueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";
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
  // The same query key the feed header and the status route use, so React Query serves all three from one
  // poll. The sidebar shows the 24 h intake behind the Event feed.
  const newsStatusQuery = useNewsStatusWithToken(session.token);
  const searchInputRef = useRef<HTMLInputElement | null>(null);
  const [shortcutsOpen, setShortcutsOpen] = useState(false);
  const awaitingGoto = useRef(false);
  const status = statusQuery.data?.data ?? null;
  const token = session.token;
  const routeContext: ShellRouteContext = {
    bootstrapError: session.bootstrapError,
    bootstrapLoading: session.bootstrapLoading,
    token,
  };
  /**
   * The shell's half of the keyboard: search, the `G` go-to prefix, the shortcut panel, and `Esc` back to the
   * feed. The reading cursor (`J`/`K`/`Enter`/`X`) belongs to the feed, and digits to its task tabs.
   */
  const handleHotkey = (event: KeyboardEvent) => {
    const target = event.target as HTMLElement | null;
    const isTyping =
      target?.tagName === "INPUT" || target?.tagName === "TEXTAREA" || target?.isContentEditable;
    if (event.metaKey || event.ctrlKey || event.altKey) return;
    if (isTyping) {
      if (event.key === "Escape") target?.blur();
      return;
    }
    if (event.key === "/") {
      event.preventDefault();
      searchInputRef.current?.focus();
      return;
    }
    if (event.key === "?") {
      event.preventDefault();
      setShortcutsOpen((open) => !open);
      return;
    }
    if (awaitingGoto.current) {
      awaitingGoto.current = false;
      if (event.key === "f") navigate(newsPath());
      if (event.key === "s") navigate(newsStatusPath());
      return;
    }
    if (event.key === "g") {
      awaitingGoto.current = true;
      return;
    }
    // Radix owns Escape while the panel is open; here it only means "leave this Event".
    if (event.key === "Escape" && !shortcutsOpen && location.pathname.startsWith("/news/events/")) {
      navigate(newsPath());
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
      navCounts: { events: newsStatusQuery.data?.funnel_24h?.received },
      onHotkey: handleHotkey,
      outletContext: routeContext,
      shortcuts: {
        items: APP_SHORTCUTS,
        onOpenChange: setShortcutsOpen,
        open: shortcutsOpen,
      },
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
    },
    routeContext,
  };
}

function isNewsRoute(pathname: string): boolean {
  return pathname === "/news" || pathname.startsWith("/news/");
}
