import { SidebarInset, SidebarProvider, SidebarTrigger } from "@shared/ui/sidebar";
import { useEffect } from "react";
import { Outlet } from "react-router-dom";

import { AppSidebar } from "./AppSidebar";
import { CockpitTopbar, type CockpitTopbarProps } from "./CockpitTopbar";
import "./cockpitShell.css";
import "./cockpitShellContract.css";

export type CockpitShellProps = {
  topbar: CockpitTopbarProps;
  onHotkey: (event: KeyboardEvent) => void;
  outletContext?: unknown;
};

export function CockpitShell({ topbar, onHotkey, outletContext }: CockpitShellProps) {
  useShellHotkeys(onHotkey);

  return (
    // The nav tree is a single destination since the Macro hard cut (#68); the rail earned no width.
    // `defaultOpen={false}` means every load starts collapsed — `shared/ui/sidebar.tsx` writes the
    // `sidebar_state` cookie but never reads it back, so a toggle is per-page, not remembered.
    <SidebarProvider className="cockpit-shell" defaultOpen={false}>
      <AppSidebar />
      <SidebarInset className="cockpit-main">
        <CockpitTopbar
          {...topbar}
          navigationTrigger={<SidebarTrigger className="topbar-sidebar-trigger" />}
        />
        <section className="center-column">
          <Outlet context={outletContext} />
        </section>
      </SidebarInset>
    </SidebarProvider>
  );
}

function useShellHotkeys(onHotkey: (event: KeyboardEvent) => void) {
  useEffect(() => {
    document.addEventListener("keydown", onHotkey);
    return () => document.removeEventListener("keydown", onHotkey);
  }, [onHotkey]);
}
