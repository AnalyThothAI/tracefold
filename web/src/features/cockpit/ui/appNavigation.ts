import { newsOiPath, newsPath, newsReviewPath, tradingPath } from "@shared/routing/paths";
import {
  EventStreamIcon,
  OpenInterestIcon,
  ReviewCheckIcon,
  TradeFlowIcon,
} from "@shared/ui/icons";
import type { LucideIcon } from "lucide-react";

export type AppNavigationItem = {
  /** A short word beside the label rather than a number: the capital lane's mode, not a volume (#207). */
  badge?: "tradingMode";
  children?: AppNavigationItem[];
  /** Which count from `AppNavigationCounts` this destination shows, if any. */
  count?: "events" | "oiFrames";
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
 * The console's whole route tree. Every entry here is a *working surface* — somewhere a reader goes to do
 * something with what the pipeline produced (#207).
 *
 * 流水线状态 used to hold the third slot and no longer does. It is a dashboard, and a healthy pipeline makes
 * it a click that returns "everything is fine": zero information for the slot it costs. The page is
 * untouched at `/news/status`; the way in is the topbar health lamp, which appears only when there is
 * something wrong and carries the failing item's own sentence to every page at once.
 *
 * 持仓异动 takes that slot. It reads the same table the feed does, filtered to
 * `admission=telemetry_deterministic` — #137's rule-judged open-interest lane, which is roughly a fifth of
 * daily volume and has never had a surface of its own.
 *
 * 交易 is the capital lane (#104). Its slot carries the ledger's `mode` as a word rather than a count: what
 * a reader needs to know before opening it is whether anything on that page is real money, and the honest
 * answer today is `PAPER`.
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
        icon: EventStreamIcon,
        isActive: (pathname) => pathname === "/news" || pathname.startsWith("/news/events"),
        label: "事件流",
        to: newsPath(),
      },
      {
        count: "oiFrames",
        icon: OpenInterestIcon,
        isActive: (pathname) => pathname === "/news/oi",
        label: "持仓异动",
        to: newsOiPath(),
      },
      {
        badge: "tradingMode",
        icon: TradeFlowIcon,
        isActive: (pathname) => pathname === "/trading",
        label: "交易",
        to: tradingPath(),
      },
      {
        icon: ReviewCheckIcon,
        isActive: (pathname) => pathname === "/news/review",
        label: "学习复盘",
        to: newsReviewPath(),
      },
    ],
  },
];
