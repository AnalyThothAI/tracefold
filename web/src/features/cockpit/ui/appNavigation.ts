import { BarChart3, BriefcaseBusiness, Newspaper, Radar, type LucideIcon } from "lucide-react";

export type AppNavigationItem = {
  children?: AppNavigationItem[];
  end?: boolean;
  icon: LucideIcon;
  label: string;
  matchPath?: string;
  to: string;
};

export type AppNavigationGroup = {
  items: AppNavigationItem[];
  label: string;
};

export const APP_NAVIGATION_GROUPS: AppNavigationGroup[] = [
  {
    label: "Research",
    items: [
      { end: true, icon: Radar, label: "Radar", to: "/" },
      { icon: BarChart3, label: "Stocks", matchPath: "/stocks/*", to: "/stocks" },
      { icon: Newspaper, label: "News", matchPath: "/news/*", to: "/news" },
      { icon: BriefcaseBusiness, label: "Macro", matchPath: "/macro/*", to: "/macro" },
    ],
  },
];
