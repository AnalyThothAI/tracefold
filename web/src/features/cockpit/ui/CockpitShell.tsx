import { ShortcutsDialog, type Shortcut } from "@shared/ui/ShortcutsDialog";
import { SidebarInset, SidebarProvider, SidebarTrigger } from "@shared/ui/sidebar";
import { useEffect, useState } from "react";
import { Outlet } from "react-router-dom";

import { AppSidebar, type AppNavigationCounts } from "./AppSidebar";
import { CockpitTopbar, type CockpitTopbarProps } from "./CockpitTopbar";
import "./cockpitShell.css";
import "./cockpitShellContract.css";

/** Below this the sidebar is a drawer the topbar trigger opens; at or above it, it is part of the frame. */
const DESKTOP_QUERY = "(min-width: 1280px)";

export type CockpitShellProps = {
  navCounts?: AppNavigationCounts;
  onHotkey: (event: KeyboardEvent) => void;
  outletContext?: unknown;
  shortcuts: {
    items: readonly Shortcut[];
    onOpenChange: (open: boolean) => void;
    open: boolean;
  };
  topbar: CockpitTopbarProps;
};

export function CockpitShell({
  navCounts,
  onHotkey,
  outletContext,
  shortcuts,
  topbar,
}: CockpitShellProps) {
  useShellHotkeys(onHotkey);
  const [sidebarOpen, setSidebarOpen] = useDesktopSidebar();

  return (
    // The route tree is two destinations (#82), and at desktop width the frame has room for both without
    // taking measure from the feed — so the sidebar is part of the page there and a drawer everywhere else.
    // `shared/ui/sidebar.tsx` writes the `sidebar_state` cookie but never reads it, so a toggle is per-page.
    <SidebarProvider className="cockpit-shell" onOpenChange={setSidebarOpen} open={sidebarOpen}>
      <AppSidebar counts={navCounts} />
      <SidebarInset className="cockpit-main">
        <CockpitTopbar
          {...topbar}
          navigationTrigger={<SidebarTrigger className="topbar-sidebar-trigger" />}
        />
        <section className="center-column">
          <Outlet context={outletContext} />
        </section>
      </SidebarInset>
      <ShortcutsDialog
        onOpenChange={shortcuts.onOpenChange}
        open={shortcuts.open}
        shortcuts={shortcuts.items}
      />
    </SidebarProvider>
  );
}

/**
 * Open at desktop width, closed below it, and re-synced when the viewport crosses the breakpoint — so a
 * rotated tablet lands in the right frame instead of keeping the state of the previous orientation. The
 * initial value is read synchronously so the first paint is already correct.
 */
function useDesktopSidebar(): [boolean, (open: boolean) => void] {
  const [open, setOpen] = useState(
    () => typeof window !== "undefined" && window.matchMedia(DESKTOP_QUERY).matches,
  );
  useEffect(() => {
    const query = window.matchMedia(DESKTOP_QUERY);
    const onChange = () => setOpen(query.matches);
    query.addEventListener("change", onChange);
    return () => query.removeEventListener("change", onChange);
  }, []);
  return [open, setOpen];
}

function useShellHotkeys(onHotkey: (event: KeyboardEvent) => void) {
  useEffect(() => {
    document.addEventListener("keydown", onHotkey);
    return () => document.removeEventListener("keydown", onHotkey);
  }, [onHotkey]);
}
