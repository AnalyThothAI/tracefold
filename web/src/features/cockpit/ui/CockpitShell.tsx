import { useMediaQuery } from "@shared/hooks/useMediaQuery";
import { Drawer } from "@shared/ui/Drawer";
import { IconButton } from "@shared/ui/IconButton";
import { PanelLeft } from "lucide-react";
import { type ReactNode, useEffect, useState } from "react";
import { Outlet } from "react-router-dom";

import { AppBottomNav } from "./AppBottomNav";
import {
  AppBrand,
  AppSidebar,
  type AppNavigationBadges,
  type AppNavigationCounts,
} from "./AppSidebar";
import { CockpitTopbar, type CockpitTopbarProps } from "./CockpitTopbar";
import "./cockpitShell.css";
import "./cockpitShellContract.css";

/** At or above this the sidebar is part of the frame; between the two it is the drawer the topbar opens. */
const DESKTOP_QUERY = "(min-width: 1280px)";
/** At or below this there is no sidebar in either form — the bottom bar carries every destination (#87). */
const PHONE_QUERY = "(max-width: 767px)";

export type CockpitShellProps = {
  navBadges?: AppNavigationBadges;
  navCounts?: AppNavigationCounts;
  outletContext?: unknown;
  routeContent?: ReactNode;
  topbar: CockpitTopbarProps;
};

/**
 * The console frame. Three widths, three navigations, one model:
 *
 *   ≥1280  the sidebar is fixed in the page, matching the approved desktop frame.
 *   768–   the same sidebar inside a left drawer the topbar trigger opens.
 *   ≤767   no sidebar in either form: a drawer charges a tap before the reader can even see where they could
 *          go, and `AppBottomNav` shows every destination at once under the thumb (#87).
 */
export function CockpitShell({
  navBadges,
  navCounts,
  outletContext,
  routeContent,
  topbar,
}: CockpitShellProps) {
  const desktop = useMediaQuery(DESKTOP_QUERY);
  const phone = useMediaQuery(PHONE_QUERY);
  const [drawerOpen, setDrawerOpen] = useState(false);
  // A rotated tablet must not keep the previous orientation's drawer open after navigation moves on-screen.
  useEffect(() => {
    if (desktop || phone) setDrawerOpen(false);
  }, [desktop, phone]);

  return (
    <div className="cockpit-shell">
      {desktop ? <AppSidebar badges={navBadges} counts={navCounts} /> : null}
      <div className="cockpit-main">
        <CockpitTopbar
          {...topbar}
          navigationTrigger={
            desktop || phone ? null : (
              <IconButton
                aria-controls="cockpit-nav-drawer"
                aria-expanded={drawerOpen}
                aria-label="切换侧栏"
                className="topbar-sidebar-trigger"
                onClick={() => setDrawerOpen((open) => !open)}
                size="sm"
                title="切换侧栏"
              >
                <PanelLeft aria-hidden />
              </IconButton>
            )
          }
        />
        {/* The route column is the page's `main`. The shadcn `SidebarInset` used to supply the landmark; a
            console without one makes a screen reader walk the sidebar and topbar on every route. */}
        <main className="center-column">{routeContent ?? <Outlet context={outletContext} />}</main>
        {phone ? <AppBottomNav /> : null}
      </div>
      {desktop || phone ? null : (
        <Drawer
          eyebrow={<AppBrand />}
          flush
          onOpenChange={setDrawerOpen}
          open={drawerOpen}
          side="left"
          title="导航"
          width={260}
        >
          <AppSidebar
            badges={navBadges}
            counts={navCounts}
            inDrawer
            onNavigate={() => setDrawerOpen(false)}
          />
        </Drawer>
      )}
    </div>
  );
}
