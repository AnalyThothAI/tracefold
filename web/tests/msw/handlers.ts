import { HttpResponse, http } from "msw";

import type { ApiMock } from "./fixtures";
import { errorBody, requestOptionsFromRequest } from "./fixtures";

export function apiHandlers(apiMock: ApiMock) {
  return [
    http.get(/.*\/api\/bootstrap$/, async () => {
      try {
        return HttpResponse.json((await apiMock.getBootstrap()) as JsonBody);
      } catch (error) {
        const { body, status } = errorBody(error);
        return HttpResponse.json(body, { status });
      }
    }),
    http.get(/.*\/api\/.*/, async ({ request }) => {
      const url = new URL(request.url);
      try {
        return HttpResponse.json(
          (await apiMock.getApi(url.pathname, requestOptionsFromRequest(request))) as JsonBody,
        );
      } catch (error) {
        const { body, status } = errorBody(error);
        return HttpResponse.json(body, { status });
      }
    }),
  ];
}

type JsonBody = Parameters<typeof HttpResponse.json>[0];
