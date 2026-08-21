import { Activity, Newspaper, Target, type LucideIcon } from "lucide-react";

export type AppNavigationItem = {
  children?: AppNavigationItem[];
  /** Which count from `AppNavigationCounts` this destination shows, if any. */
  count?: "events";
  icon: LucideIcon;
  /** Whether the current path belongs to this destination. Event detail belongs to the feed, not to itself. */
  isActive: (pathname: string) => boolean;
  label: string;
  /** The one non-count signal a destination may carry: the pipeline health behind it. */
  signal?: "health";
  to: string;
};

export type AppNavigationGroup = {
  items: AppNavigationItem[];
  label: string;
};

/**
 * The console's whole route tree. News is the only product (#68), so its three surfaces are the navigation:
 * the Event feed, 学习复盘 — immutable human evidence and candidate evaluation — and the
 * pipeline status behind both. Event detail lives under the feed and highlights it.
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
        icon: Newspaper,
        isActive: (pathname) => pathname === "/news" || pathname.startsWith("/news/events"),
        label: "事件流",
        to: "/news",
      },
      {
        icon: Target,
        isActive: (pathname) => pathname === "/news/review",
        label: "学习复盘",
        to: "/news/review",
      },
      {
        icon: Activity,
        isActive: (pathname) => pathname === "/news/status",
        label: "流水线状态",
        signal: "health",
        to: "/news/status",
      },
    ],
  },
];
