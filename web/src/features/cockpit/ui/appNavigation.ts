import { newsMarketPath, newsPath, tradingPath } from "@shared/routing/paths";
import { EventStreamIcon, TelemetryPulseIcon, TradeFlowIcon } from "@shared/ui/icons";
import type { LucideIcon } from "lucide-react";

export type AppNavigationItem = {
  children?: AppNavigationItem[];
  /** Which count from `AppNavigationCounts` this destination shows, if any. */
  count?: "events";
  /** What the count is counting, as the link's own tooltip. Every destination counts a different thing. */
  countTitle?: string;
  icon: LucideIcon;
  /** Whether the current path belongs to this destination. Event detail belongs to the feed, not to itself. */
  isActive: (pathname: string) => boolean;
  label: string;
  to: string;
};

export type AppNavigationGroup = {
  items: AppNavigationItem[];
  label: string;
};

/**
 * The console's whole route tree, in one group of working surfaces (#256, #553 PR-1).
 *
 * `Workbench` is where a reader goes to do something with what the pipeline produced. `System · 数据健康`
 * held one entry — the OI frame audit — and went with it: 市场事实 is not a health page. It is the reading
 * surface for the market observations OpenNews sends, which since #553 PR-1 are stored facts rather than
 * Events, and a reader opens it to see what the venues reported, not to find out whether the pipeline is
 * telling the truth. That second question is the topbar health lamp's, on every page.
 *
 * 流水线状态 holds no slot either. It is a dashboard, and a healthy pipeline makes it a click that
 * returns "everything is fine": zero information for the slot it costs. The page is untouched at
 * `/news/status`; the way in is the topbar health lamp, which is on every page, states its level in a dot
 * rather than in prose, and carries the failing item's own sentence in its accessible name and popover.
 *
 * 学习复盘 held a workbench slot and no longer exists (#256). The artifact drops it, and the ReviewDesk it
 * fronted is a CLI lane — `tracefold news review queue / evidence / submit` — writing the same
 * `news_reviews` rows the learning lane reads. One path in, not two.
 *
 * Alpha 判定 held a workbench slot beside 交易 and no longer exists (#460). It read the same
 * `GET /api/trading/cases` the Trading workbench reads, and the one thing it showed that the workbench
 * did not — a Case's frozen per-check evidence — moved into the Case card there.
 *
 * One model, three presentations: the desktop sidebar, the tablet drawer and the phone tab bar all read this
 * list, so a destination cannot exist in one and be missing from another.
 */
export const APP_NAVIGATION_GROUPS: AppNavigationGroup[] = [
  {
    label: "Workbench",
    items: [
      {
        count: "events",
        countTitle: "过去 24 小时收到",
        icon: EventStreamIcon,
        isActive: (pathname) => pathname === "/news" || pathname.startsWith("/news/events"),
        label: "事件流",
        to: newsPath(),
      },
      {
        /*
         * No count. `/api/news/status` reports the editorial funnel and nothing about market intake since
         * #553 PR-1 — the per-kind figures are on the page itself, read from the facts by
         * `/api/news/market`. A sidebar number would have to come from a second poll of that endpoint for
         * a figure the destination states in full the moment it opens.
         */
        icon: TelemetryPulseIcon,
        isActive: (pathname) => pathname === "/news/market",
        label: "市场事实",
        to: newsMarketPath(),
      },
      {
        /*
         * No count and no badge. Inheriting Alpha 判定's `count: "cases"` was tried and reverted in #460
         * — the row is 204px and `交易`, a `flex: 1` label with `text-overflow: ellipsis`, came out as a
         * single clipped glyph beside it. The `tradingEnvironment` badge that replaced it printed the
         * lane's last-Case clock and execution mode, and paying for it meant every News route polling
         * `/api/trading/status` every 15 s for two words the desk itself states (#537 PR-5).
         */
        icon: TradeFlowIcon,
        isActive: (pathname) => pathname === "/trading",
        label: "交易",
        to: tradingPath(),
      },
    ],
  },
];
