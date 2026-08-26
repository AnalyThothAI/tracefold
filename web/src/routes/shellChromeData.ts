import type { AppSession } from "@app/useAppSession";
import {
  useCockpitStatusQuery,
  type CockpitHealth,
  type CockpitShellProps,
  type CockpitTopbarFigure,
} from "@features/cockpit";
import {
  HEALTH_ITEM_KEYS,
  healthItemEyebrow,
  healthLevelLabel,
  optionalDuration,
  useNewsStatusWithToken,
  type NewsStatus,
} from "@features/news/shell";
import { useTradingStatusWithToken, type TradingStatus } from "@features/trading/shell";
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
  [/^\/news$/, "事件流"],
  [/^\/trading$/, "交易 · 模拟仓"],
];

export type ShellRouteContext = {
  bootstrapError: boolean;
  bootstrapFailure: unknown;
  bootstrapLoading: boolean;
  retryBootstrap: () => unknown;
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
  /*
   * The capital lane's mode for the 交易 slot. Its own key and its own 15 s rhythm — the frame reads it
   * so the badge is right on every route, and the trading page shares the same cache entry rather than
   * opening a second poll of the same endpoint.
   */
  const tradingStatusQuery = useTradingStatusWithToken(session.token);
  const status = statusQuery.data?.data ?? null;
  const token = session.token;
  const routeContext: ShellRouteContext = {
    bootstrapError: session.bootstrapError,
    bootstrapFailure: session.bootstrapFailure,
    bootstrapLoading: session.bootstrapLoading,
    retryBootstrap: session.retryBootstrap,
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
      navBadges: {
        // Uppercased because it is a mode word, not prose: `PAPER` beside 交易 answers "is any of this real
        // money" before the reader spends a click finding out.
        tradingMode: tradingStatusQuery.data?.readiness.mode?.toUpperCase(),
      },
      outletContext: routeContext,
      topbar: {
        eventFeed: location.pathname === "/news",
        health: healthLamp(
          newsStatusQuery.data,
          newsStatusQuery.isError,
          location.pathname === "/news",
        ),
        // The visual contract makes the topbar a route context strip. Every value is an already-served
        // material fact from the two status reads this shell shares with the pages; switching routes never
        // starts a new request or asks the browser to invent a KPI.
        figures: topbarFigures(location.pathname, newsStatusQuery.data, tradingStatusQuery.data),
        onRefresh:
          location.pathname === "/news" ? undefined : () => void queryClient.invalidateQueries(),
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

/** The two or three facts that identify the surface at a glance, using only status fields already in cache. */
export function topbarFigures(
  pathname: string,
  newsStatus?: NewsStatus,
  tradingStatus?: TradingStatus,
  nowMs = Date.now(),
): CockpitTopbarFigure[] {
  if (pathname === "/trading") {
    const readiness = tradingStatus?.readiness;
    const budget = tradingStatus?.budget;
    return [
      { label: "MODE", text: readiness?.mode.toUpperCase() },
      {
        label: "LIVE READY",
        text: readiness?.live_readiness.toUpperCase(),
        tone: readiness?.live_ready === false ? "caution" : undefined,
      },
      {
        label: "ORDERS TODAY",
        text: budget ? `${budget.orders_today} / ${budget.max_orders_per_day}` : undefined,
      },
    ];
  }

  if (pathname === "/news/oi") {
    return [
      {
        label: "OI FRAMES 24H",
        tone: "accent",
        value: newsStatus?.pipeline.telemetry_received_24h,
      },
      oiCaseFigure(tradingStatus, nowMs),
    ];
  }

  if (pathname === "/news/status") {
    return [
      { label: "EVENTS 24H", tone: "accent", value: newsStatus?.pipeline.events_24h },
      {
        label: "QUEUE P95",
        text: loadedDuration(newsStatus?.pipeline.queue_lag_p95_ms),
      },
    ];
  }

  if (pathname === "/news") {
    return [
      { label: "PUSHED 24H", tone: "accent", value: newsStatus?.delivery.sent_24h },
      { label: "E2E P50", text: loadedDuration(newsStatus?.delivery.e2e_p50_ms) },
    ];
  }

  return [
    { label: "PUSHED 24H", tone: "accent", value: newsStatus?.delivery.sent_24h },
    { label: "E2E P95", text: loadedDuration(newsStatus?.delivery.e2e_p95_ms) },
  ];
}

function oiCaseFigure(
  tradingStatus: TradingStatus | undefined,
  nowMs: number,
): CockpitTopbarFigure {
  if (!tradingStatus) return { label: "CASES TODAY", value: undefined };

  const dayKey = tradingStatus.counts.funnel_day_key;
  const today = new Date(nowMs).toISOString().slice(0, 10);
  const stale = dayKey !== today;
  return {
    label: stale
      ? `CASES · ${/^\d{4}-\d{2}-\d{2}$/.test(dayKey) ? dayKey.slice(5) : "STALE"}`
      : "CASES TODAY",
    // Sparse counter maps omit zeroes; once the status document exists, absence is the ledger's zero.
    value: tradingStatus.counts.funnel_today?.case_created ?? 0,
    ...(stale
      ? {
          title: dayKey ? `UTC ${dayKey}` : "UTC 日期未知",
          tone: "caution" as const,
        }
      : {}),
  };
}

function loadedDuration(value: number | null | undefined): string | undefined {
  if (value == null) return undefined;
  return optionalDuration(value);
}

/**
 * The server's `health` as the topbar lamp's structural prop.
 *
 * Healthy status normally returns `null`; the approved Event feed passes `showHealthy` so its compact
 * `流水线` affordance remains present. Nothing is computed here — the level, stage levels and sentences are
 * server values, and the only local decisions are which item is the worst and what to call each stage.
 *
 * A failed read is its own state and must not read as health, and it takes precedence over any status
 * still in cache. React Query keeps the last good `data` through a failed refetch, so gating this on
 * "failed *and* nothing cached" would leave a console whose 15 s poll started 5xx-ing showing the last
 * `ok` health — silently, on every route, because this lamp is the console's only health signal and the
 * topbar's other indicator watches a different endpoint (`/api/status`).
 */
function healthLamp(
  status: NewsStatus | undefined,
  failed: boolean,
  showHealthy = false,
): CockpitHealth | null {
  if (failed) {
    return {
      buttonText: showHealthy ? "流水线" : undefined,
      headline: "流水线状态暂不可用",
      items: [],
      level: "bad",
      summary: "读取流水线状态失败",
      to: newsStatusPath(),
    };
  }
  const health = status?.health;
  if (!health || (!showHealthy && health.overall !== "warn" && health.overall !== "bad"))
    return null;
  const items = HEALTH_ITEM_KEYS.map((key) => ({
    key,
    label: healthItemEyebrow(key),
    level: health[key].level,
    summary: health[key].summary_zh,
  }));
  const worst = items.find((item) => item.level === health.overall);
  return {
    buttonText: showHealthy ? "流水线" : undefined,
    headline: health.overall === "ok" ? "流水线状态" : `流水线${healthLevelLabel(health.overall)}`,
    items,
    level: health.overall,
    summary: health.overall === "ok" ? "流水线" : (worst?.summary ?? ""),
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
