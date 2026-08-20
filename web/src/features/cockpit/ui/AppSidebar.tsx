import {
  Sidebar,
  SidebarContent,
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  useSidebar,
} from "@shared/ui/sidebar";
import { NavLink, useLocation } from "react-router-dom";

import { APP_NAVIGATION_GROUPS, type AppNavigationItem } from "./appNavigation";
import "./AppSidebar.css";

export type AppNavigationCounts = { events?: number };

export function AppSidebar({ counts }: { counts?: AppNavigationCounts }) {
  return (
    <Sidebar
      aria-label="Application sidebar"
      className="cockpit-app-sidebar"
      collapsible="offcanvas"
      variant="sidebar"
    >
      <SidebarHeader className="cockpit-app-sidebar-header">
        <div className="cockpit-app-sidebar-brand">
          <span className="cockpit-app-sidebar-mark" aria-hidden>
            T
          </span>
          <span>
            <b>Tracefold</b>
            <small>Research Workbench</small>
          </span>
        </div>
      </SidebarHeader>
      <SidebarContent>
        <nav aria-label="Primary navigation" className="cockpit-app-sidebar-nav">
          {APP_NAVIGATION_GROUPS.map((group) => (
            <SidebarGroup key={group.label}>
              <SidebarGroupLabel asChild>
                <h2 className="cockpit-app-sidebar-group-heading">{group.label}</h2>
              </SidebarGroupLabel>
              <SidebarGroupContent>
                <SidebarMenu>
                  {group.items.map((item) => (
                    <AppSidebarItem counts={counts} item={item} key={item.to} />
                  ))}
                </SidebarMenu>
              </SidebarGroupContent>
            </SidebarGroup>
          ))}
        </nav>
      </SidebarContent>
    </Sidebar>
  );
}

function AppSidebarItem({
  counts,
  item,
}: {
  counts?: AppNavigationCounts;
  item: AppNavigationItem;
}) {
  const { pathname } = useLocation();
  const active = item.isActive(pathname);
  const { isMobile, setOpenMobile } = useSidebar();
  const Icon = item.icon;
  const count = item.count ? counts?.[item.count] : undefined;

  return (
    <SidebarMenuItem>
      <SidebarMenuButton asChild isActive={active} tooltip={item.label}>
        <NavLink
          onClick={() => {
            if (isMobile) setOpenMobile(false);
          }}
          to={item.to}
        >
          <Icon aria-hidden />
          <span className="cockpit-app-sidebar-label">{item.label}</span>
          {/*
           * The count is decoration on the link, not part of what it is: folding it into the accessible name
           * would make the destination announce itself differently every three seconds. The same number is
           * announced properly by the feed's labelled 24 h funnel.
           */}
          {count == null ? null : (
            <span aria-hidden className="cockpit-app-sidebar-count" title="过去 24 小时收到">
              {new Intl.NumberFormat("zh-CN").format(count)}
            </span>
          )}
        </NavLink>
      </SidebarMenuButton>
    </SidebarMenuItem>
  );
}
