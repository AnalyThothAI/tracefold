import {
  createBrowserRouter,
  createMemoryRouter,
  Navigate,
  type RouteObject,
} from "react-router-dom";

import { RouteErrorElement, RouteNotFoundElement } from "./routeErrorElement";
import { SearchShellRoute, ShellChromeRoute, ShellRoute } from "./shell.route";
import { useShellRouteContext } from "./shellRouteContext";

export type AppRouter = ReturnType<typeof createBrowserRouter>;
export type AppRouterFactory = () => AppRouter;

export function createAppRouteObjects(): RouteObject[] {
  return [
    {
      element: <ShellChromeRoute />,
      errorElement: <RouteErrorElement />,
      children: [
        {
          element: <ShellRoute />,
          children: [
            {
              path: "token/:targetType/:targetId",
              lazy: () => import("./token-target.route"),
            },
            {
              path: "news",
              lazy: () => import("./news.route"),
            },
            {
              path: "news/events/:eventId",
              lazy: () => import("./news.route"),
            },
            {
              path: "news/status",
              lazy: () => import("./news.route"),
            },
            {
              path: "macro",
              lazy: async () => {
                const { MacroOverviewPage } = await import("@features/macro");
                return {
                  Component: function MacroOverviewRoute() {
                    const { bootstrapError, bootstrapLoading, token } = useShellRouteContext();
                    return (
                      <MacroOverviewPage
                        bootstrapError={bootstrapError}
                        bootstrapLoading={bootstrapLoading}
                        token={token}
                      />
                    );
                  },
                };
              },
            },
            {
              path: "macro/overview",
              lazy: async () => {
                const { MacroOverviewPage } = await import("@features/macro");
                return {
                  Component: function MacroOverviewAliasRoute() {
                    const { bootstrapError, bootstrapLoading, token } = useShellRouteContext();
                    return (
                      <MacroOverviewPage
                        bootstrapError={bootstrapError}
                        bootstrapLoading={bootstrapLoading}
                        token={token}
                      />
                    );
                  },
                };
              },
            },
            {
              path: "macro/:modulePath",
              lazy: () => import("./macro.route"),
            },
            {
              index: true,
              element: <Navigate replace to="/news" />,
            },
          ],
        },
        {
          element: <SearchShellRoute />,
          children: [
            {
              path: "search",
              lazy: () => import("./search.route"),
            },
          ],
        },
      ],
    },
    {
      path: "*",
      element: <RouteNotFoundElement />,
    },
  ];
}

export function createAppBrowserRouter(): AppRouter {
  return createBrowserRouter(createAppRouteObjects());
}

export function createAppMemoryRouter(
  options: { initialEntries?: string[]; initialIndex?: number } = {},
): AppRouter {
  return createMemoryRouter(createAppRouteObjects(), options);
}
