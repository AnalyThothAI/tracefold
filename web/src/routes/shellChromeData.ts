import type { AppSession } from "@app/useAppSession";
import { APP_SHORTCUTS, useCockpitStatusQuery, type CockpitShellProps } from "@features/cockpit";
import { useNewsStatusWithToken } from "@features/news/shell";
import { newsPath, newsStatusPath } from "@shared/routing/paths";
import { searchWithOptionalPrefix } from "@shared/routing/searchParams";
import { useQueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";

/** How long a `g` stays armed. Long enough to be a chord, short enough that a stray `g` is forgotten. */
const GOTO_PREFIX_MS = 1_200;

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
  const awaitingGoto = useRef<number | null>(null);
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
    // The go-to prefix is armed only briefly: a stray `g` must not swallow whatever the reader types next,
    // and must not still be waiting minutes later.
    if (awaitingGoto.current != null && Date.now() - awaitingGoto.current < GOTO_PREFIX_MS) {
      awaitingGoto.current = null;
      if (event.key === "f") navigate(newsPath());
      if (event.key === "s") navigate(newsStatusPath());
      return;
    }
    awaitingGoto.current = null;
    if (event.key === "g") {
      awaitingGoto.current = Date.now();
      return;
    }
    // Radix owns Escape while the panel is open; here it only means "leave this Event" — back to the feed
    // the reader came from, filters and tab intact, exactly like the 返回事件流 link beside it.
    if (event.key === "Escape" && !shortcutsOpen && location.pathname.startsWith("/news/events/")) {
      const feedSearch = (location.state as { feedSearch?: string } | null)?.feedSearch;
      navigate(feedSearch ? `${newsPath()}?${feedSearch}` : newsPath());
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
        // #87: the two numbers the operator checks without opening a page. Both are already-served fields —
        // no derived rate, and nothing that would need a market-data lane the pipeline does not have.
        figures: [
          { label: "PUSHED 24H", value: newsStatusQuery.data?.delivery?.sent_24h },
          {
            label: "MISSED",
            tone: "caution" as const,
            value: newsStatusQuery.data?.pipeline?.labeled_missed_24h,
          },
        ],
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
