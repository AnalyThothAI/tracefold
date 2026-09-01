import type { AppSession } from "@app/useAppSession";
import {
  useCockpitStatusQuery,
  type CockpitHealth,
  type CockpitShellProps,
  type CockpitTopbarFigure,
} from "@features/cockpit";
import {
  HEALTH_ITEM_KEYS,
  healthItemTitle,
  healthLevelLabel,
  optionalDuration,
  useNewsStatusWithToken,
  type NewsStatus,
} from "@features/news/shell";
import { useTradingStatusWithToken, type TradingStatus } from "@features/trading/shell";
import { newsPath, newsStatusPath } from "@shared/routing/paths";
import { searchWithOptionalPrefix } from "@shared/routing/searchParams";
import { useLocation, useNavigate } from "react-router-dom";

/** What the frame calls each surface. The page keeps its own `h1`; this is the "where am I" line. */
const PAGE_TITLES: Array<[RegExp, string]> = [
  [/^\/news\/events\//, "事件详情"],
  [/^\/news\/status$/, "流水线状态"],
  [/^\/news\/oi$/, "OI 来源与准入审计"],
  [/^\/news$/, "事件流"],
  [/^\/trading$/, "Alpha 与执行"],
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
  const statusQuery = useCockpitStatusQuery({ token: session.token });
  // The same query key the feed header and the status route use, so React Query serves all three from one
  // poll. The sidebar shows the 24 h intake behind the Event feed.
  const newsStatusQuery = useNewsStatusWithToken(session.token);
  /* The shared Alpha/execution status read gives every route the same explicit disabled-mode badge. */
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
    const next = topbarNewsSearchParams(searchText);
    navigate({ pathname: newsPath(), search: searchWithOptionalPrefix(next) });
  };
  return {
    cockpitShellProps: {
      /*
       * The three reads the frame itself owns. A route's own query is not folded in: it polls every few
       * seconds, and a line that reappeared on every poll would stop meaning "still loading".
       */
      busy:
        statusQuery.isPending || newsStatusQuery.isPending || tradingStatusQuery.isPending
          ? Boolean(token)
          : false,
      navCounts: {
        /*
         * No `cases` here since #460. The count existed for Alpha 判定's slot; that page is gone, and
         * 交易 inherited its badge rather than its number — the two together clip a 204px row's label to
         * a single glyph. `CASES 24H` still leads the Trading page's own figure row.
         */
        events: newsStatusQuery.data?.funnel_24h?.received,
        // #207: the deterministic OI lane's own 24 h intake, the same figure the monitor's telemetry band
        // leads with. Received, not pushed — the destination is the whole lane, not its output.
        oiFrames: newsStatusQuery.data?.pipeline?.telemetry_received_24h,
      },
      navBadges: {
        tradingEnvironment: tradingStatusQuery.data
          ? `${tradingStatusQuery.data.decision.state} · ${tradingStatusQuery.data.execution.mode}`
          : undefined,
      },
      outletContext: routeContext,
      topbar: {
        health: healthLamp(newsStatusQuery.data, newsStatusQuery.isError),
        // The visual contract makes the topbar a route context strip. Every value is an already-served
        // material fact from the two status reads this shell shares with the pages; switching routes never
        // starts a new request or asks the browser to invent a KPI.
        figures: topbarFigures(location.pathname, newsStatusQuery.data, tradingStatusQuery.data),
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

/** A topbar submit starts a new News task; hidden filters from the previous task never cross this seam. */
export function topbarNewsSearchParams(searchText: string): URLSearchParams {
  const next = new URLSearchParams();
  const query = searchText.trim();
  if (query) next.set("q", query);
  next.set("outcome", "all");
  next.set("hours", "168");
  return next;
}

/** The two or three facts that identify the surface at a glance, using only status fields already in cache. */
export function topbarFigures(
  pathname: string,
  newsStatus?: NewsStatus,
  tradingStatus?: TradingStatus,
  nowMs = Date.now(),
): CockpitTopbarFigure[] {
  if (pathname === "/trading") {
    const decision = tradingStatus?.decision;
    const execution = tradingStatus?.execution;
    return [
      {
        label: "DECISION",
        text: decision?.state,
        tone: decision?.state === "FAULTED" ? "caution" : undefined,
      },
      {
        label: "EXECUTION",
        text: execution?.mode,
        tone: execution?.ready ? undefined : "caution",
      },
      {
        label: "SIGNALS 24H",
        value: tradingStatus?.counts.signals_24h,
      },
    ];
  }

  if (pathname === "/news/oi") {
    // Parsed, not pushed (#458). The lane has no push decision of its own any more, and a chrome figure
    // that stayed on `telemetry_push_24h` would have read a permanent 0 as "the feed went quiet".
    return [
      {
        label: "PARSED 24H",
        tone: "accent",
        value: newsStatus?.pipeline.telemetry_parsed_24h,
      },
      oiDailyFigure(tradingStatus, nowMs),
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

function oiDailyFigure(
  tradingStatus: TradingStatus | undefined,
  _nowMs: number,
): CockpitTopbarFigure {
  if (!tradingStatus) return { label: "24h 成案 · Signal", text: undefined };

  return {
    label: "24h 成案 · Signal",
    text: `${tradingStatus.counts.cases_24h} · ${tradingStatus.counts.signals_24h}`,
  };
}

function loadedDuration(value: number | null | undefined): string | undefined {
  if (value == null) return undefined;
  return optionalDuration(value);
}

/**
 * The server's `health` as the topbar lamp's structural prop.
 *
 * The lamp is on every route and stays present while healthy (#256): 流水线状态 holds no navigation slot,
 * so hiding the affordance on the healthy path would make the page unreachable exactly when a reader wants
 * to confirm nothing is wrong. Nothing is computed here — the level, stage levels and sentences are
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
      buttonText: "流水线",
      headline: "流水线状态暂不可用",
      items: [],
      level: "bad",
      summary: "读取流水线状态失败",
      to: newsStatusPath(),
    };
  }
  const health = status?.health;
  if (!health) return null;
  const items: CockpitHealth["items"] = HEALTH_ITEM_KEYS.map((key) => ({
    key,
    label: healthItemTitle(key),
    level: health[key].level,
    summary: `${healthLevelLabel(health[key].level)} · ${health[key].summary_zh}`,
  }));
  const instruments = status?.instruments;
  if (instruments?.last_snapshot_ms != null) {
    const dangling = instruments.dangling_aliases;
    items.push({
      key: "instruments",
      label: "标的表",
      level: null,
      summary:
        dangling > 0
          ? `${new Intl.NumberFormat("zh-CN").format(dangling)} 个别名未落标的表`
          : `${new Intl.NumberFormat("zh-CN").format(instruments.trading)} 份交易合约`,
    });
  }
  const worstStage = HEALTH_ITEM_KEYS.find((key) => health[key].level === health.overall);
  return {
    buttonText: "流水线",
    headline: health.overall === "ok" ? "流水线状态" : `流水线${healthLevelLabel(health.overall)}`,
    items,
    level: health.overall,
    summary: health.overall === "ok" ? "流水线" : worstStage ? health[worstStage].summary_zh : "",
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
