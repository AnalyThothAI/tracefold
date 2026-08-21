import { useMediaQuery } from "@shared/hooks/useMediaQuery";
import { CommandPalette, type Command } from "@shared/ui/CommandPalette";
import { Drawer } from "@shared/ui/Drawer";
import { IconButton } from "@shared/ui/IconButton";
import { ShortcutsDialog, type Shortcut } from "@shared/ui/ShortcutsDialog";
import { Toast } from "@shared/ui/Toast";
import { PanelLeft } from "lucide-react";
import { useEffect, useState } from "react";
import { Outlet } from "react-router-dom";

import { AppBottomNav } from "./AppBottomNav";
import {
  AppBrand,
  AppSidebar,
  type AppNavigationCounts,
  type AppNavigationLevel,
} from "./AppSidebar";
import { CockpitTopbar, type CockpitTopbarProps } from "./CockpitTopbar";
import "./cockpitShell.css";
import "./cockpitShellContract.css";

/** At or above this the sidebar is part of the frame; between the two it is the drawer the topbar opens. */
const DESKTOP_QUERY = "(min-width: 1280px)";
/** At or below this there is no sidebar in either form — the bottom bar carries every destination (#87). */
const PHONE_QUERY = "(max-width: 767px)";

export type CockpitShellProps = {
  /** What ⌘K can do, assembled by the shell route from what the destinations already expose. */
  commands?: Command[];
  navCounts?: AppNavigationCounts;
  navStatusLevel?: AppNavigationLevel;
  onHotkey: (event: KeyboardEvent) => void;
  outletContext?: unknown;
  palette?: { onOpenChange: (open: boolean) => void; open: boolean };
  shortcuts: {
    items: readonly Shortcut[];
    onOpenChange: (open: boolean) => void;
    open: boolean;
  };
  /** The console's single copy confirmation. Routes push through `ShellRouteContext.copy`, never their own. */
  toast?: string | null;
  topbar: CockpitTopbarProps;
};

/**
 * The console frame. Three widths, three navigations, one model:
 *
 *   ≥1280  the sidebar is part of the page — the route tree is three destinations and the frame has room.
 *   768–   the same sidebar inside a left drawer the topbar trigger opens.
 *   ≤767   no sidebar in either form: a drawer charges a tap before the reader can even see where they could
 *          go, and `AppBottomNav` shows every destination at once under the thumb (#87).
 */
export function CockpitShell({
  commands,
  navCounts,
  navStatusLevel,
  onHotkey,
  outletContext,
  palette,
  shortcuts,
  toast,
  topbar,
}: CockpitShellProps) {
  useShellHotkeys(onHotkey);
  const desktop = useMediaQuery(DESKTOP_QUERY);
  const phone = useMediaQuery(PHONE_QUERY);
  // One control, two meanings: at desktop it folds the in-frame sidebar away for readers who want the whole
  // column; at tablet width it opens the same navigation as a drawer.
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [drawerOpen, setDrawerOpen] = useState(false);
  // A rotated tablet must not keep the previous orientation's frame, and an open drawer must not survive the
  // width at which the sidebar is already on screen.
  useEffect(() => {
    if (desktop || phone) setDrawerOpen(false);
  }, [desktop, phone]);

  return (
    <div className="cockpit-shell">
      {desktop && sidebarOpen ? (
        <AppSidebar counts={navCounts} statusLevel={navStatusLevel} />
      ) : null}
      <div className="cockpit-main">
        <CockpitTopbar
          {...topbar}
          navigationTrigger={
            phone ? null : (
              <IconButton
                aria-controls={desktop ? undefined : "cockpit-nav-drawer"}
                aria-expanded={desktop ? undefined : drawerOpen}
                aria-label="切换侧栏"
                aria-pressed={desktop ? sidebarOpen : undefined}
                className="topbar-sidebar-trigger"
                onClick={() =>
                  desktop ? setSidebarOpen((open) => !open) : setDrawerOpen((open) => !open)
                }
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
        <main className="center-column">
          <Outlet context={outletContext} />
        </main>
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
            counts={navCounts}
            inDrawer
            onNavigate={() => setDrawerOpen(false)}
            statusLevel={navStatusLevel}
          />
        </Drawer>
      )}
      <ShortcutsDialog
        onOpenChange={shortcuts.onOpenChange}
        open={shortcuts.open}
        shortcuts={shortcuts.items}
      />
      {palette ? (
        <CommandPalette
          commands={commands ?? []}
          onOpenChange={palette.onOpenChange}
          open={palette.open}
        />
      ) : null}
      <Toast message={toast ?? null} />
    </div>
  );
}

function useShellHotkeys(onHotkey: (event: KeyboardEvent) => void) {
  useEffect(() => {
    document.addEventListener("keydown", onHotkey);
    return () => document.removeEventListener("keydown", onHotkey);
  }, [onHotkey]);
}
