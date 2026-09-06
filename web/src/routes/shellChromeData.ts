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
import { newsPath, newsStatusPath } from "@shared/routing/paths";
import { searchWithOptionalPrefix } from "@shared/routing/searchParams";
import { useLocation, useNavigate } from "react-router-dom";

/** What the frame calls each surface. The page keeps its own `h1`; this is the "where am I" line. */
const PAGE_TITLES: Array<[RegExp, string]> = [
  [/^\/news\/events\//, "事件详情"],
  [/^\/news\/status$/, "流水线状态"],
  [/^\/news\/market$/, "市场事实"],
  [/^\/news\/wallets$/, "链上钱包"],
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
       * The two reads the frame itself owns. A route's own query is not folded in: it polls every few
       * seconds, and a line that reappeared on every poll would stop meaning "still loading".
       *
       * `/api/trading/status` was a third, on every News route, for a sidebar badge and two topbar
       * figures. It made every reader of the Event feed poll the execution Runtime's readiness every
       * 15 s to render a clock and the word `paper` (#537 PR-5).
       */
      busy: statusQuery.isPending || newsStatusQuery.isPending ? Boolean(token) : false,
      navCounts: {
        /*
         * No `cases` here since #460. The count existed for Alpha 判定's slot; that page is gone, and
         * 交易 inherited its badge rather than its number — the two together clip a 204px row's label to
         * a single glyph. `CASES 24H` still leads the Trading page's own figure row.
         */
        events: newsStatusQuery.data?.funnel_24h?.received,
        /*
         * No market count since #553 PR-1. `/api/news/status` reports the editorial funnel only — market
         * intake is a fact `/api/news/market` reports per kind — and a second poll of that endpoint from
         * the frame would buy one number the destination itself prints in full.
         */
      },
      outletContext: routeContext,
      topbar: {
        health: healthLamp(newsStatusQuery.data, newsStatusQuery.isError),
        // The visual contract makes the topbar a route context strip. Every value is an already-served
        // material fact from the two status reads this shell shares with the pages; switching routes never
        // starts a new request or asks the browser to invent a KPI.
        figures: topbarFigures(location.pathname, newsStatusQuery.data),
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

/**
 * The two facts that identify the surface at a glance, using only status fields already in cache.
 *
 * Every one of them comes from the News pipeline status this shell already polls. The three Trading
 * figures that were here — the lane's last Case clock, the execution mode and the 24 h Signal count —
 * are the first three things `/trading` states on the page itself, and printing them in the frame is
 * what made every News route poll `/api/trading/status` every 15 s (#537 PR-5). `/trading` therefore
 * carries no chrome figure: the desk is a page about one thing, and the frame does not restate it.
 */
export function topbarFigures(pathname: string, newsStatus?: NewsStatus): CockpitTopbarFigure[] {
  if (pathname === "/trading") return [];

  /*
   * 市场事实 carries no chrome figure (#553 PR-1). It read `pipeline.telemetry_parsed_24h`, which is not a
   * status field any more: market intake is reported per kind by `/api/news/market`, from the facts, and
   * the page leads with exactly that strip. A frame figure here could only be a second poll of the page's
   * own endpoint, or an invented number.
   */
  if (pathname === "/news/market") return [];

  /*
   * 链上钱包 carries none either, for the same reason (#572 PR-3): every figure it could show is read from
   * `/api/news/wallets`, which is the page's own endpoint, and the page leads with exactly those tiles.
   */
  if (pathname === "/news/wallets") return [];

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
