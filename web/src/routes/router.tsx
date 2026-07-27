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
              path: "news",
              lazy: () => import("./news.route"),
            },
            {
              path: "news/stories/:storyId",
              lazy: () => import("./news.route"),
            },
            {
              path: "news/brief",
              lazy: () => import("./news.route"),
            },
            {
              path: "news/sources",
              lazy: () => import("./news.route"),
            },
            {
              path: "macro",
              lazy: async () => {
                const { MacroOverviewPage } = await import("@features/macro");
                return {
                  Component: function MacroOverviewRoute() {
                    const { token } = useShellRouteContext();
                    return <MacroOverviewPage token={token} />;
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
                ["rates-fed", "rates_fed"],
                ["economy-inflation", "economy_inflation"],
                ["liquidity-funding", "liquidity_funding"],
                ["credit", "credit"],
                ["volatility", "volatility"],
                ["cross-asset", "cross_asset"],
              ] as const
            ).map(([path, moduleId]) => ({
              path: `macro/${path}`,
              lazy: async () => {
                const { MacroModulePage } = await import("@features/macro");
                return {
                  Component: function MacroModuleRoute() {
                    const { token } = useShellRouteContext();
                    return <MacroModulePage moduleId={moduleId} token={token} />;
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
