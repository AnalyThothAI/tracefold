import { MACRO_MODULE_DEFINITIONS, MacroModulePage } from "@features/macro";
import { useParams } from "react-router-dom";

import { RouteNotFoundElement } from "./routeErrorElement";
import { useShellRouteContext } from "./shellRouteContext";

export function Component() {
  const { modulePath } = useParams<{ modulePath: string }>();
  const definition = MACRO_MODULE_DEFINITIONS.find(
    (candidate) => candidate.routeSegment === modulePath,
  );
  const { bootstrapError, bootstrapLoading, token } = useShellRouteContext();
  if (!definition) return <RouteNotFoundElement />;
  return (
    <MacroModulePage
      bootstrapError={bootstrapError}
      bootstrapLoading={bootstrapLoading}
      moduleId={definition.id}
      token={token}
    />
  );
}
