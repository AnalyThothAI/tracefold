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
    <SidebarProvider className="cockpit-shell">
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
