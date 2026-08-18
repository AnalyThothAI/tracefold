import { CockpitShell } from "@features/cockpit";
import { Outlet } from "react-router-dom";

import { useAppRouteSession } from "./routeSession";
import { ShellChromeContext, useShellChrome } from "./shellChromeContext";
import { useShellChromeData } from "./shellChromeData";

export function ShellChromeRoute() {
  const session = useAppRouteSession();
  const chrome = useShellChromeData(session);

  return (
    <ShellChromeContext.Provider value={chrome}>
      <Outlet />
    </ShellChromeContext.Provider>
  );
}

export function ShellRoute() {
  const { cockpitShellProps } = useShellChrome();

  return <CockpitShell {...cockpitShellProps} />;
}
