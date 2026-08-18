import { AppRouteSessionProvider } from "@routes/AppRouteSessionProvider";
import { createAppBrowserRouter, type AppRouterFactory } from "@routes/router";
import { RouteFallback } from "@shared/ui/RouteFallback";
import { useMemo } from "react";
import { RouterProvider } from "react-router-dom";

import { useAppSession } from "./useAppSession";

export function CockpitApp({
  createRouter = createAppBrowserRouter,
}: {
  createRouter?: AppRouterFactory;
}) {
  const session = useAppSession();
  const router = useMemo(() => createRouter(), [createRouter]);

  return (
    <AppRouteSessionProvider session={session}>
      <RouterProvider router={router} fallbackElement={<RouteFallback />} />
    </AppRouteSessionProvider>
  );
}
