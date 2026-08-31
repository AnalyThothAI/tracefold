import { newsAlphaPath, newsOiPath, newsPath, tradingPath } from "@shared/routing/paths";
import {
  EventStreamIcon,
  LeverageGaugeIcon,
  TelemetryPulseIcon,
  TradeFlowIcon,
} from "@shared/ui/icons";
import type { LucideIcon } from "lucide-react";

export type AppNavigationItem = {
  /** Alpha decision state and explicit execution mode beside the Trading destination. */
  badge?: "tradingEnvironment";
  children?: AppNavigationItem[];
  /** Which count from `AppNavigationCounts` this destination shows, if any. */
  count?: "cases" | "events" | "oiFrames";
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
 * Alpha 判定 takes a workbench slot beside it. It and OI 来源与准入审计 read the same deterministic lane and answer
 * different questions with different thresholds: what Alpha decided, versus whether the telemetry
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
        countTitle: "过去 24 小时收到",
        icon: EventStreamIcon,
        isActive: (pathname) => pathname === "/news" || pathname.startsWith("/news/events"),
        label: "事件流",
        to: newsPath(),
      },
      {
        /*
         * The Signal lane's own 24 h Case count comes from the existing `/api/trading/status` read. It was
         * left blank when the slot landed on the
         * theory that the honest figure needed a fourth poll; it does not, and an empty right edge beside
         * three numbered siblings reads as "nothing came through here" rather than as "not counted".
         *
         * Cases, not frames: the destination is what the lane decided, and the frame population is the OI
         * audit's own count one group below.
         */
        count: "cases",
        /* Not 「过去 24 小时」 like its two siblings: their fields are named `*_24h` and the window is part
           of the field, where the Case aggregate follows the Signal lane's published `window_hours` and a
           tooltip that hard-coded 24 would be wrong the first time an operator changed it. */
        countTitle: "Alpha 成案 · 账本滚动窗口",
        icon: LeverageGaugeIcon,
        isActive: (pathname) => pathname === "/news/alpha",
        label: "Alpha 判定",
        to: newsAlphaPath(),
      },
      {
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
