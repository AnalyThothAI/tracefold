import type { AppSession } from "@app/useAppSession";
import {
  APP_SHORTCUTS,
  useCockpitStatusQuery,
  type AppNavigationLevel,
  type CockpitShellProps,
} from "@features/cockpit";
import {
  NEWS_REVIEW_DEFAULT_HOURS,
  hitFigure,
  labelCommand,
  useNewsReviewWithToken,
  useNewsStatusWithToken,
} from "@features/news/shell";
import { useCopyToast } from "@shared/hooks/useCopyToast";
import { newsPath, newsReviewPath, newsStatusPath } from "@shared/routing/paths";
import { searchWithOptionalPrefix } from "@shared/routing/searchParams";
import type { Command } from "@shared/ui/CommandPalette";
import { useQueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";

/** How long a `g` stays armed. Long enough to be a chord, short enough that a stray `g` is forgotten. */
const GOTO_PREFIX_MS = 1_200;

/** What the frame calls each surface. The page keeps its own `h1`; this is the "where am I" line. */
const PAGE_TITLES: Array<[RegExp, string]> = [
  [/^\/news\/events\//, "事件详情"],
  [/^\/news\/review$/, "命中复盘"],
  [/^\/news\/status$/, "流水线状态"],
  [/^\/news$/, "新闻事件流"],
];

export type ShellRouteContext = {
  bootstrapError: boolean;
  bootstrapLoading: boolean;
  /** The console's one clipboard affordance, so every route confirms a copy through the same toast. */
  copy: (text: string, note: string) => void;
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
  // #88: the review summary behind the topbar figure. Same 60 s query key the 命中复盘 route uses, so the
  // page and the chrome share one poll instead of each asking for a 168 h aggregate.
  const newsReviewQuery = useNewsReviewWithToken(session.token, NEWS_REVIEW_DEFAULT_HOURS);
  const searchInputRef = useRef<HTMLInputElement | null>(null);
  const [shortcutsOpen, setShortcutsOpen] = useState(false);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const awaitingGoto = useRef<number | null>(null);
  const toast = useCopyToast();
  const status = statusQuery.data?.data ?? null;
  const token = session.token;
  const routeContext: ShellRouteContext = {
    bootstrapError: session.bootstrapError,
    bootstrapLoading: session.bootstrapLoading,
    copy: toast.copy,
    token,
  };
  /**
   * The shell's half of the keyboard: the palette, search, the `G` go-to prefix, the shortcut panel, and
   * `Esc` back to the feed. The reading cursor (`J`/`K`/`Enter`/`Space`/`X`) belongs to the feed, and digits
   * to its task tabs.
   */
  const handleHotkey = (event: KeyboardEvent) => {
    const target = event.target as HTMLElement | null;
    const isTyping =
      target?.tagName === "INPUT" || target?.tagName === "TEXTAREA" || target?.isContentEditable;
    // ⌘K is the one binding that fires while a field has focus: it is how the reader leaves the field.
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
      event.preventDefault();
      setPaletteOpen((open) => !open);
      return;
    }
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
      if (event.key === "r") navigate(newsReviewPath());
      if (event.key === "s") navigate(newsStatusPath());
      return;
    }
    awaitingGoto.current = null;
    if (event.key === "g") {
      awaitingGoto.current = Date.now();
      return;
    }
    // Radix owns Escape while a panel is open; here it only means "leave this Event" — back to the feed the
    // reader came from, filters and tab intact, exactly like the 返回事件流 link beside it.
    if (
      event.key === "Escape" &&
      !shortcutsOpen &&
      !paletteOpen &&
      location.pathname.startsWith("/news/events/")
    ) {
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
    navigate({ pathname: newsPath(), search: searchWithOptionalPrefix(next) });
  };
  const health = newsStatusQuery.data?.health?.overall;

  return {
    cockpitShellProps: {
      commands: paletteCommands({
        copy: toast.copy,
        eventId: currentEventId(location.pathname),
        navigate,
        watchlist: newsStatusQuery.data?.watchlist ?? [],
      }),
      navCounts: { events: newsStatusQuery.data?.funnel_24h?.received },
      navStatusLevel: health === "warn" || health === "bad" ? (health as AppNavigationLevel) : "ok",
      onHotkey: handleHotkey,
      outletContext: routeContext,
      palette: { onOpenChange: setPaletteOpen, open: paletteOpen },
      shortcuts: { items: APP_SHORTCUTS, onOpenChange: setShortcutsOpen, open: shortcutsOpen },
      toast: toast.message,
      topbar: {
        // #87: the two numbers the operator checks without opening a page. Both are already-served fields —
        // no derived rate, and nothing that would need a market-data lane the pipeline does not have.
        figures: [
          {
            label: "PUSHED 24H",
            tone: "accent" as const,
            value: newsStatusQuery.data?.delivery?.sent_24h,
          },
          {
            label: "MISSED",
            tone: "caution" as const,
            value: newsStatusQuery.data?.pipeline?.labeled_missed_24h,
          },
          // #88: a hit rate arrives with its denominator or not at all — never a bare `0%`.
          {
            label: "HIT 1H",
            text: newsReviewQuery.data
              ? hitFigure(
                  newsReviewQuery.data.summary.hit_1h_pct,
                  newsReviewQuery.data.summary.hit_1h_n,
                )
              : undefined,
            title: "最近 7 天，方向判断在事件后 1H 的命中率与样本量",
          },
        ],
        onOpenPalette: () => setPaletteOpen(true),
        onRefresh: () => void queryClient.invalidateQueries(),
        search: {
          inputRef: searchInputRef,
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
 * What ⌘K can do (design proposal ①). Every entry is something the console already offers somewhere else —
 * a destination, a task tab, a symbol filter, a label command — collapsed into one keystroke. Nothing here
 * writes to the server: the label entries copy the same CLI command the detail page hands over.
 *
 * The symbol jumps come from the watchlist the server already serves. The browser owns no symbol table, so
 * the palette can only offer symbols something upstream has already named.
 */
function paletteCommands({
  copy,
  eventId,
  navigate,
  watchlist,
}: {
  copy: (text: string, note: string) => void;
  eventId: string | null;
  navigate: (to: string) => void;
  watchlist: readonly string[];
}): Command[] {
  const go = (to: string) => () => navigate(to);
  return [
    {
      glyph: "⌘",
      hint: "所有事件与它们的去向",
      id: "nav-feed",
      kind: "nav",
      label: "打开事件流",
      run: go(newsPath()),
    },
    {
      glyph: "⌘",
      hint: "覆盖率、方向命中与待核对队列",
      id: "nav-review",
      kind: "nav",
      label: "打开命中复盘",
      run: go(newsReviewPath()),
    },
    {
      glyph: "⌘",
      hint: "五段管线健康度与 24 小时去向",
      id: "nav-status",
      kind: "nav",
      label: "打开流水线状态",
      run: go(newsStatusPath()),
    },
    {
      glyph: "⌗",
      hint: "只看送达读者的事件",
      id: "filter-pushed",
      kind: "filter",
      label: "只看已推送",
      run: go(`${newsPath()}?outcome=pushed`),
    },
    {
      glyph: "⌗",
      hint: "模型或规则判定不推的事件",
      id: "filter-held",
      kind: "filter",
      label: "只看被拦截",
      run: go(`${newsPath()}?outcome=held`),
    },
    {
      glyph: "⌗",
      hint: "还在排队或投递中的事件",
      id: "filter-pending",
      kind: "filter",
      label: "只看处理中",
      run: go(`${newsPath()}?outcome=pending`),
    },
    {
      glyph: "⌗",
      hint: "回到默认的 24 小时全部事件",
      id: "filter-clear",
      kind: "filter",
      label: "清除筛选",
      run: go(newsPath()),
    },
    ...watchlist.map((symbol) => ({
      glyph: "⇥",
      hint: "关注列表标的",
      id: `goto-${symbol}`,
      kind: "goto",
      label: `跳到 ${symbol}`,
      run: go(`${newsPath()}?symbol=${encodeURIComponent(symbol)}`),
    })),
    ...(eventId
      ? ([
          {
            glyph: "✓",
            hint: "复制 CLI 命令，不写库",
            id: "label-good",
            kind: "label",
            label: "把当前事件标为判得对",
            run: () => copy(labelCommand(eventId, "good"), "已复制「判得对」标注命令"),
          },
          {
            glyph: "✓",
            hint: "复制 CLI 命令，不写库",
            id: "label-noise",
            kind: "label",
            label: "把当前事件标为不该推",
            run: () => copy(labelCommand(eventId, "noise"), "已复制「不该推」标注命令"),
          },
          {
            glyph: "✓",
            hint: "复制 CLI 命令，不写库",
            id: "label-missed",
            kind: "label",
            label: "把当前事件标为漏推",
            run: () => copy(labelCommand(eventId, "missed"), "已复制「漏推」标注命令"),
          },
          {
            glyph: "✓",
            hint: "发布门的边界集",
            id: "label-must-push",
            kind: "label",
            label: "把当前事件标为必须推",
            run: () => copy(labelCommand(eventId, "must_push"), "已复制「必须推」标注命令"),
          },
        ] satisfies Command[])
      : []),
    {
      glyph: "⇥",
      hint: "浏览器打开事件流并聚焦搜索框",
      id: "goto-search",
      kind: "goto",
      label: "搜索事件",
      run: () => {
        navigate(newsPath());
        window.setTimeout(() => document.getElementById("news-search-input")?.focus(), 0);
      },
    },
  ];
}

function currentEventId(pathname: string): string | null {
  const match = /^\/news\/events\/(.+)$/.exec(pathname);
  return match ? decodeURIComponent(match[1]) : null;
}

function pageTitle(pathname: string): string {
  return PAGE_TITLES.find(([pattern]) => pattern.test(pathname))?.[1] ?? "新闻事件流";
}

function isNewsRoute(pathname: string): boolean {
  return pathname === "/news" || pathname.startsWith("/news/");
}
