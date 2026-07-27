import { setAuthToken } from "@lib/api/client";
import { cleanup } from "@testing-library/react";
import { createApiMock, resetApiMock, type ApiMock } from "@tests/msw/fixtures";
import { apiHandlers } from "@tests/msw/handlers";
import { mockBootstrap, mockLiveRadarRoute } from "@tests/msw/scenarios";
import { server } from "@tests/msw/server";
import { resetSocketScenario, socketScenario } from "@tests/socket/socketScenarios";
import type { ReactNode } from "react";
import { vi } from "vitest";

export const apiMock = createApiMock();

vi.mock("@shared/socket/IntelSocketProvider", async () => {
  const React = await vi.importActual<typeof import("react")>("react");
  return {
    IntelSocketProvider: ({ children }: { children: ReactNode }) =>
      React.createElement(React.Fragment, null, children),
  };
});

vi.mock("@shared/socket/socketContext", () => ({
  useSocketSnapshot: () => ({
    lastMessageAt: socketScenario.lastMessageAt,
    status: socketScenario.status,
  }),
}));

vi.mock("@shared/socket/useMarketSubscription", () => ({
  useMarketSubscription: () => undefined,
}));

export function setupAppRouteTest(configure: (mock: ApiMock) => void = mockLiveRadarRoute) {
  cleanup();
  setAuthToken(null);
  resetApiMock(apiMock);
  resetSocketScenario();
  server.use(...apiHandlers(apiMock));
  mockBootstrap(apiMock);
  configure(apiMock);
}
