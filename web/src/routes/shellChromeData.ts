import type { AppSession } from "@app/useAppSession";
import {
  useCockpitStatusQuery,
  type AppNavigationLevel,
  type CockpitShellProps,
} from "@features/cockpit";
import { useNewsStatusWithToken } from "@features/news/shell";
import { newsPath } from "@shared/routing/paths";
import { searchWithOptionalPrefix } from "@shared/routing/searchParams";
import { useQueryClient } from "@tanstack/react-query";
import { useLocation, useNavigate } from "react-router-dom";

/** What the frame calls each surface. The page keeps its own `h1`; this is the "where am I" line. */
const PAGE_TITLES: Array<[RegExp, string]> = [
  [/^\/news\/events\//, "事件详情"],
  [/^\/news\/review$/, "学习复盘"],
  [/^\/news\/status$/, "流水线状态"],
  [/^\/news$/, "新闻事件流"],
];

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
  const status = statusQuery.data?.data ?? null;
  const token = session.token;
  const routeContext: ShellRouteContext = {
    bootstrapError: session.bootstrapError,
    bootstrapLoading: session.bootstrapLoading,
    token,
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
    navigate({ pathname: newsPath(), search: searchWithOptionalPrefix(next) });
  };
  const health = newsStatusQuery.data?.health?.overall;

  return {
    cockpitShellProps: {
      navCounts: { events: newsStatusQuery.data?.funnel_24h?.received },
      navStatusLevel: health === "warn" || health === "bad" ? (health as AppNavigationLevel) : "ok",
      outletContext: routeContext,
      topbar: {
        // #87: the two numbers the operator checks without opening a page. Both are already-served fields —
        // no derived rate, and nothing that would need a market-data lane the pipeline does not have.
        figures: [
          {
            label: "PUSHED 24H",
            tone: "accent" as const,
            value: newsStatusQuery.data?.delivery?.sent_24h,
          },
        ],
        onRefresh: () => void queryClient.invalidateQueries(),
        search: {
          onSubmitQuery: submitTopbarSearch,
          query: currentSearchQuery,
        },
        status: {
          configReady: Boolean(token),
          status,
          statusError: statusQuery.isError,
          statusLoading: Boolean(token) && statusQuery.isPending,
        },
        title: pageTitle(location.pathname),
      },
    },
    routeContext,
  };
}

function pageTitle(pathname: string): string {
  return PAGE_TITLES.find(([pattern]) => pattern.test(pathname))?.[1] ?? "新闻事件流";
}

function isNewsRoute(pathname: string): boolean {
  return pathname === "/news" || pathname.startsWith("/news/");
}
