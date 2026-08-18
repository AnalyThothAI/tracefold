import { setAuthToken } from "@lib/api/client";
import { cleanup } from "@testing-library/react";
import { createApiMock, resetApiMock, type ApiMock } from "@tests/msw/fixtures";
import { apiHandlers } from "@tests/msw/handlers";
import { mockBootstrap, mockAppRoutes } from "@tests/msw/scenarios";
import { server } from "@tests/msw/server";

export const apiMock = createApiMock();

export function setupAppRouteTest(configure: (mock: ApiMock) => void = mockAppRoutes) {
  cleanup();
  setAuthToken(null);
  resetApiMock(apiMock);
  server.use(...apiHandlers(apiMock));
  mockBootstrap(apiMock);
  configure(apiMock);
}
