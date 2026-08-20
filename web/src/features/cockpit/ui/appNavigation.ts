import { Activity, Newspaper, type LucideIcon } from "lucide-react";

export type AppNavigationItem = {
  children?: AppNavigationItem[];
  /** Which count from `CockpitShellProps.navCounts` this destination shows, if any. */
  count?: "events";
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
 * The console's whole route tree. News is the only product (#68), so its two surfaces are the navigation:
 * the Event feed and the pipeline status behind it. Event detail lives under the feed and highlights it.
 */
export const APP_NAVIGATION_GROUPS: AppNavigationGroup[] = [
  {
    label: "Research",
    items: [
      {
        count: "events",
        icon: Newspaper,
        isActive: (pathname) => pathname === "/news" || pathname.startsWith("/news/events"),
        label: "事件流",
        to: "/news",
      },
      {
        icon: Activity,
        isActive: (pathname) => pathname === "/news/status",
        label: "流水线状态",
        to: "/news/status",
      },
    ],
  },
];
