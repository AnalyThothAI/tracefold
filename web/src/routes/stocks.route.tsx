import { StocksRadarPage } from "@features/stocks";

import { useShellRouteContext } from "./shellRouteContext";

export function Component() {
  const context = useShellRouteContext();

  return (
    <StocksRadarPage
      token={context.token}
      windowKey={context.windowKey}
      onWindowChange={context.updateWindow}
    />
  );
}
