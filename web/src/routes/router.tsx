import { createBrowserRouter, createMemoryRouter, type RouteObject } from "react-router-dom";

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
              path: "stocks",
              lazy: () => import("./stocks.route"),
            },
            {
              path: "watchlist",
              lazy: () => import("./watchlist.route"),
            },
            {
              path: "news",
              lazy: () => import("./news.route"),
            },
            {
              path: "news/stories/:storyId",
              lazy: () => import("./news.route"),
            },
            {
              path: "macro",
              lazy: async () => {
                const { MacroLiveEvidencePage } = await import("@features/macro");
                return {
                  Component: function MacroLiveDashboardRoute() {
                    const { token } = useShellRouteContext();
                    return <MacroLiveEvidencePage token={token} viewId="dashboard" />;
                  },
                };
              },
            },
            {
              path: "macro/research",
              lazy: async () => {
                const { MacroResearchPage } = await import("@features/macro");
                return {
                  Component: function MacroResearchRoute() {
                    const { token } = useShellRouteContext();
                    return <MacroResearchPage token={token} />;
                  },
                };
              },
            },
            ...(
              [
                ["overview", "overview"],
                ["rates-inflation", "rates-inflation"],
                ["growth-labor", "growth-labor"],
                ["liquidity-funding", "liquidity-funding"],
                ["credit", "credit"],
                ["cross-asset", "cross-asset"],
              ] as const
            ).map(([path, viewId]) => ({
              path: `macro/${path}`,
              lazy: async () => {
                const { MacroLiveEvidencePage } = await import("@features/macro");
                return {
                  Component: function MacroLiveDetailRoute() {
                    const { token } = useShellRouteContext();
                    return <MacroLiveEvidencePage token={token} viewId={viewId} />;
                  },
                };
              },
            })),
            {
              index: true,
              lazy: () => import("./live.route"),
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
