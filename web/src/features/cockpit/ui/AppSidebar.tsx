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
import { NavLink, useMatch } from "react-router-dom";

import { APP_NAVIGATION_GROUPS, type AppNavigationItem } from "./appNavigation";
import "./AppSidebar.css";

export function AppSidebar() {
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
            P
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
                    <AppSidebarItem item={item} key={item.to} />
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

function AppSidebarItem({ item }: { item: AppNavigationItem }) {
  const active = Boolean(
    useMatch({
      end: item.end ?? false,
      path: item.matchPath ?? item.to,
    }),
  );
  const { isMobile, setOpenMobile } = useSidebar();
  const Icon = item.icon;

  return (
    <SidebarMenuItem>
      <SidebarMenuButton asChild isActive={active} tooltip={item.label}>
        <NavLink
          end={item.end}
          onClick={() => {
            if (isMobile) setOpenMobile(false);
          }}
          to={item.to}
        >
          <Icon aria-hidden />
          <span>{item.label}</span>
        </NavLink>
      </SidebarMenuButton>
    </SidebarMenuItem>
  );
}
