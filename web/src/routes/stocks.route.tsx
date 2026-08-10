import {
  parseStocksRouteState,
  serializeStocksRouteState,
  StocksRadarPage,
} from "@features/stocks";
import { useSearchParams } from "react-router-dom";

import { useShellRouteContext } from "./shellRouteContext";

export function Component() {
  const context = useShellRouteContext();
  const [searchParams, setSearchParams] = useSearchParams();
  const routeState = parseStocksRouteState(searchParams);

  return (
    <StocksRadarPage
      token={context.token}
      windowKey={routeState.window}
      onWindowChange={(window) =>
        setSearchParams(serializeStocksRouteState({ ...routeState, window }))
      }
    />
  );
}
