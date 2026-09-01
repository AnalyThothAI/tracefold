import { newsOiPath, newsPath, tradingPath } from "@shared/routing/paths";
import { EventStreamIcon, TelemetryPulseIcon, TradeFlowIcon } from "@shared/ui/icons";
import type { LucideIcon } from "lucide-react";

export type AppNavigationItem = {
  /** Alpha decision state and explicit execution mode beside the Trading destination. */
  badge?: "tradingEnvironment";
  children?: AppNavigationItem[];
  /** Which count from `AppNavigationCounts` this destination shows, if any. */
  count?: "events" | "oiFrames";
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
 * The console's whole route tree, in the two groups the v7 artifact draws (#256).
 *
 * `WORKBENCH` is where a reader goes to do something with what the pipeline produced. `SYSTEM · 数据健康` is
 * where they go to find out whether the pipeline itself is telling the truth — a different question, asked
 * at a different time, and mixing the two put a frame-parse audit one tab away from a reading surface.
 *
 * 流水线状态 holds no slot in either group. It is a dashboard, and a healthy pipeline makes it a click that
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
 * did not — a Case's frozen per-check evidence — moved into the Case card there. OI 来源与准入审计 stays
 * separate because it answers a different question with different thresholds: whether the telemetry
 * itself parsed and cleared the gates, not what Alpha then decided.
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
         * A word, not a volume, and still only one of them after #460 folded Alpha 判定's slot into this
         * destination. Inheriting that slot's `count: "cases"` was tried and reverted: the row is 204px,
         * the badge already spends ~85px of it, and `交易` — a `flex: 1` label with `text-overflow:
         * ellipsis` — came out as a single clipped glyph beside `7 RUNNING · disabled`. The count is the
         * lesser of the two here anyway: "is any of this real money" is what a reader needs before
         * opening it, and `CASES 24H` is the first figure on the page itself.
         */
        badge: "tradingEnvironment",
        icon: TradeFlowIcon,
        isActive: (pathname) => pathname === "/trading",
        label: "交易",
        to: tradingPath(),
      },
    ],
  },
  {
    label: "System · 数据健康",
    items: [
      {
        count: "oiFrames",
        countTitle: "过去 24 小时收到",
        icon: TelemetryPulseIcon,
        isActive: (pathname) => pathname === "/news/oi",
        label: "OI 来源与准入审计",
        to: newsOiPath(),
      },
    ],
  },
];
