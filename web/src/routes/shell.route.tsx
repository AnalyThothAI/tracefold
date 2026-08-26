import { CockpitShell } from "@features/cockpit";
import * as PageState from "@shared/ui/PageState";
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
  const { cockpitShellProps, routeContext } = useShellChrome();
  const routeContent = routeContext.bootstrapError ? (
    <div className="cockpit-session-state">
      <PageState.Error
        error={routeContext.bootstrapFailure ?? new Error("无法建立控制台会话")}
        onRetry={() => void routeContext.retryBootstrap()}
      />
    </div>
  ) : routeContext.bootstrapLoading ? (
    <div className="cockpit-session-state">
      <PageState.Loading label="正在建立控制台会话" layout="route" rows={5} />
    </div>
  ) : undefined;

  return <CockpitShell {...cockpitShellProps} routeContent={routeContent} />;
}
