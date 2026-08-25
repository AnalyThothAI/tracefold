import { TradingPage } from "@features/trading";

import { useShellRouteContext } from "./shellRouteContext";

export function Component() {
  const { token } = useShellRouteContext();
  return <TradingPage token={token} />;
}
