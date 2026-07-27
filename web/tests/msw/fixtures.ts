import { ApiError, type RequestOptions } from "@lib/api/client";
import type { ApiResponse, BootstrapData } from "@lib/types";
import { vi } from "vitest";

export type ApiMock = {
  getApiImpl: (path: string, options?: RequestOptions) => Promise<unknown>;
  getBootstrapImpl: () => Promise<unknown>;
  postApiImpl: (path: string, options?: RequestOptions) => Promise<unknown>;
  readApiImpl: (path: string, options?: RequestOptions) => Promise<unknown>;
  writeApiImpl: (path: string, options?: RequestOptions) => Promise<unknown>;
  getApi: ApiMockFunction<[string, RequestOptions?]>;
  getBootstrap: ApiMockFunction<[]>;
  postApi: ApiMockFunction<[string, RequestOptions?]>;
  readApi: ApiMockFunction<[string, RequestOptions?]>;
  writeApi: ApiMockFunction<[string, RequestOptions?]>;
};

type ApiMockFunction<Args extends unknown[]> = {
  (...args: Args): Promise<unknown>;
  mockClear: () => void;
  mock: { calls: Args[] };
};

export function createApiMock(): ApiMock {
  const mock = {} as ApiMock;
  let readApiImpl: ApiMock["getApiImpl"] = async (path: string) => {
    throw new Error(`unexpected path ${path}`);
  };
  let writeApiImpl: ApiMock["postApiImpl"] = async (path: string) => {
    throw new Error(`unexpected post ${path}`);
  };
  Object.defineProperty(mock, "getApiImpl", {
    get: () => readApiImpl,
    set: (value: ApiMock["getApiImpl"]) => {
      readApiImpl = value;
    },
  });
  Object.defineProperty(mock, "readApiImpl", {
    get: () => readApiImpl,
    set: (value: ApiMock["readApiImpl"]) => {
      readApiImpl = value;
    },
  });
  Object.defineProperty(mock, "postApiImpl", {
    get: () => writeApiImpl,
    set: (value: ApiMock["postApiImpl"]) => {
      writeApiImpl = value;
    },
  });
  Object.defineProperty(mock, "writeApiImpl", {
    get: () => writeApiImpl,
    set: (value: ApiMock["writeApiImpl"]) => {
      writeApiImpl = value;
    },
  });
  mock.getBootstrapImpl = async () => {
    throw new Error("unexpected bootstrap request");
  };
  mock.getApi = vi.fn((path: string, options?: RequestOptions) =>
    mock.getApiImpl(path, options),
  ) as ApiMock["getApi"];
  mock.getBootstrap = vi.fn(() => mock.getBootstrapImpl()) as ApiMock["getBootstrap"];
  mock.postApi = vi.fn((path: string, options?: RequestOptions) =>
    mock.postApiImpl(path, options),
  ) as ApiMock["postApi"];
  mock.readApi = mock.getApi;
  mock.writeApi = mock.postApi;
  return mock;
}

export function resetApiMock(mock: ApiMock): void {
  mock.getApi.mockClear();
  mock.getBootstrap.mockClear();
  mock.postApi.mockClear();
  mock.writeApiImpl = async () => ok({ updated: true });
}

export function ok<T>(data: T): ApiResponse<T> {
  return { ok: true, data };
}

export function defaultBootstrap(): ApiResponse<BootstrapData> {
  return ok({ ws_token: "secret", replay_limit: 25 });
}

export function requestOptionsFromRequest(request: Request): RequestOptions {
  const url = new URL(request.url);
  const token = authorizationToken(request);
  const params = paramsFromUrl(url);
  return {
    ...(token ? { token } : {}),
    ...(Object.keys(params).length ? { params } : {}),
  };
}

export function paramsFromUrl(url: URL): NonNullable<RequestOptions["params"]> {
  const params: NonNullable<RequestOptions["params"]> = {};
  for (const [key, value] of url.searchParams.entries()) {
    params[key] = coerceParam(key, value);
  }
  return params;
}

export function errorBody(error: unknown): { body: { ok: false; error: string }; status: number } {
  if (error instanceof ApiError) {
    return { body: { ok: false, error: error.message }, status: error.status };
  }
  const message = error instanceof Error ? error.message : "unexpected test handler error";
  return { body: { ok: false, error: message }, status: 500 };
}

function authorizationToken(request: Request): string | undefined {
  const value = request.headers.get("authorization") ?? "";
  return value.startsWith("Bearer ") ? value.slice("Bearer ".length) : undefined;
}

function coerceParam(key: string, value: string): string | number | boolean {
  if (value === "true") return true;
  if (value === "false") return false;
  if (key === "limit") return Number(value);
  return value;
}
