import {
  createBrowserRouter,
  createMemoryRouter,
  Navigate,
  type RouteObject,
} from "react-router-dom";

import { RouteErrorElement, RouteNotFoundElement } from "./routeErrorElement";
import { ShellChromeRoute, ShellRoute } from "./shell.route";

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
              path: "news/oi",
              lazy: () => import("./news.route"),
            },
            {
              path: "news/symbols/:base",
              lazy: () => import("./news.route"),
            },
            {
              path: "trading",
              lazy: () => import("./trading.route"),
            },
            {
              index: true,
              element: <Navigate replace to="/news" />,
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
