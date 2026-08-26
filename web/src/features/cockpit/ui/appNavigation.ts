import { newsLeveragePath, newsOiPath, newsPath, tradingPath } from "@shared/routing/paths";
import {
  EventStreamIcon,
  LeverageGaugeIcon,
  TelemetryPulseIcon,
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
 * 杠杆异动 takes a workbench slot beside it. It and OI 遥测审计 read the same deterministic lane and answer
 * different questions with different thresholds: what the capital lane decided, versus whether the telemetry
 * itself parsed and cleared the push gates.
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
        /*
         * No count. The artifact shows one, and the honest figure behind it — how many cases are live right
         * now — is not in any read the frame already makes; adding a fourth poll to decorate a link is the
         * wrong trade. The page leads with the same four figures in a labelled row.
         */
        icon: LeverageGaugeIcon,
        isActive: (pathname) => pathname === "/news/leverage",
        label: "杠杆异动",
        to: newsLeveragePath(),
      },
      {
        badge: "tradingMode",
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
        icon: TelemetryPulseIcon,
        isActive: (pathname) => pathname === "/news/oi",
        label: "OI 遥测审计",
        to: newsOiPath(),
      },
    ],
  },
];
