import type { AppSession } from "@app/useAppSession";
import {
  useCockpitStatusQuery,
  type CockpitHealth,
  type CockpitShellProps,
} from "@features/cockpit";
import {
  HEALTH_ITEM_KEYS,
  healthItemEyebrow,
  healthLevelLabel,
  useNewsStatusWithToken,
  type NewsStatus,
} from "@features/news/shell";
import { newsPath, newsStatusPath } from "@shared/routing/paths";
import { searchWithOptionalPrefix } from "@shared/routing/searchParams";
import { useQueryClient } from "@tanstack/react-query";
import { useLocation, useNavigate } from "react-router-dom";

/** What the frame calls each surface. The page keeps its own `h1`; this is the "where am I" line. */
const PAGE_TITLES: Array<[RegExp, string]> = [
  [/^\/news\/events\//, "事件详情"],
  [/^\/news\/review$/, "学习复盘"],
  [/^\/news\/status$/, "流水线状态"],
  [/^\/news\/oi$/, "持仓异动监控"],
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
  return {
    cockpitShellProps: {
      navCounts: {
        events: newsStatusQuery.data?.funnel_24h?.received,
        // #207: the deterministic OI lane's own 24 h intake, the same figure the monitor's telemetry band
        // leads with. Received, not pushed — the destination is the whole lane, not its output.
        oiFrames: newsStatusQuery.data?.pipeline?.telemetry_received_24h,
      },
      outletContext: routeContext,
      topbar: {
        health: healthLamp(newsStatusQuery.data, newsStatusQuery.isError),
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

/**
 * The server's `health` as the topbar lamp's structural prop, or `null` while there is nothing to say.
 *
 * `ok` and `off` both return `null`: the lamp exists to interrupt, and a light that is always on is one the
 * reader stops seeing. Nothing is computed here — the level, the four stage levels and every sentence are
 * server values, and the only local decisions are which item is the worst and what to call each stage.
 *
 * A failed read is its own state and must not read as health, and it takes precedence over any status
 * still in cache. React Query keeps the last good `data` through a failed refetch, so gating this on
 * "failed *and* nothing cached" would leave a console whose 15 s poll started 5xx-ing showing the last
 * `ok` health — silently, on every route, because this lamp is the console's only health signal and the
 * topbar's other indicator watches a different endpoint (`/api/status`).
 */
function healthLamp(status: NewsStatus | undefined, failed: boolean): CockpitHealth | null {
  if (failed) {
    return {
      headline: "流水线状态暂不可用",
      items: [],
      level: "bad",
      summary: "读取流水线状态失败",
      to: newsStatusPath(),
    };
  }
  const health = status?.health;
  if (!health || (health.overall !== "warn" && health.overall !== "bad")) return null;
  const items = HEALTH_ITEM_KEYS.map((key) => ({
    key,
    label: healthItemEyebrow(key),
    level: health[key].level,
    summary: health[key].summary_zh,
  }));
  const worst = items.find((item) => item.level === health.overall);
  return {
    headline: `流水线${healthLevelLabel(health.overall)}`,
    items,
    level: health.overall,
    summary: worst?.summary ?? "",
    to: newsStatusPath(),
  };
}

function pageTitle(pathname: string): string {
  /*
   * The token page names the token (#207 PR-W1). It is the one route whose title is data rather than a
   * label, and a static entry would have left every symbol's topbar reading 新闻事件流 — which is what the
   * regenerated baseline caught.
   */
  const symbol = /^\/news\/symbols\/([^/]+)$/.exec(pathname);
  if (symbol) return `代币 · ${decodeURIComponent(symbol[1]).toUpperCase().replace(/^XYZ-/, "")}`;
  return PAGE_TITLES.find(([pattern]) => pattern.test(pathname))?.[1] ?? "新闻事件流";
}

function isNewsRoute(pathname: string): boolean {
  return pathname === "/news" || pathname.startsWith("/news/");
}
