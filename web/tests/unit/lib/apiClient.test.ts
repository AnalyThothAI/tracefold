import { getApi, setAuthToken } from "@lib/api/client";
import { afterEach, describe, expect, it, vi } from "vitest";

afterEach(() => {
  setAuthToken(null);
  vi.unstubAllGlobals();
});

describe("API client request contract", () => {
  it.each([
    { explicit: undefined, global: "global-token", expected: "Bearer global-token" },
    { explicit: "request-token", global: "global-token", expected: "Bearer request-token" },
  ])("uses the request token before the session token", async ({ explicit, global, expected }) => {
    const fetchMock = stubFetch(jsonResponse({ ok: true, data: { value: 1 } }));
    setAuthToken(global);

    await getApi("/api/news/feed", { token: explicit });

    expect(requestHeaders(fetchMock, 0).get("authorization")).toBe(expected);
  });

  it("keeps zero and false params while dropping absent and empty params", async () => {
    const fetchMock = stubFetch(jsonResponse({ ok: true, data: {} }));

    await getApi("/api/news/feed", {
      params: {
        enabled: false,
        empty: "",
        limit: 0,
        missing: undefined,
        nil: null,
        q: "macro surprise",
      },
    });

    const url = requestUrl(fetchMock, 0);
    expect([...url.searchParams.entries()]).toEqual([
      ["enabled", "false"],
      ["limit", "0"],
      ["q", "macro surprise"],
    ]);
  });
});

describe("API client response contract", () => {
  it("preserves HTTP status when an error response is not JSON", async () => {
    stubFetch(new Response("Internal Server Error", { status: 500 }));

    await expect(getApi("/api/news/feed")).rejects.toMatchObject({
      name: "ApiError",
      message: "Internal Server Error",
      status: 500,
    });
  });

  it("preserves the server error code on a JSON envelope", async () => {
    stubFetch(jsonResponse({ ok: false, data: null, error: "invalid_filter" }, 400));

    await expect(getApi("/api/news/feed")).rejects.toMatchObject({
      name: "ApiError",
      code: "invalid_filter",
      message: "invalid_filter",
      status: 400,
    });
  });
});

describe("API client ETag contract", () => {
  it("revalidates with the stored ETag and reuses the cached envelope on 304", async () => {
    const cached = { ok: true, data: { event_id: "evt-1" } };
    const fetchMock = stubFetch(
      jsonResponse(cached, 200, { etag: '\"feed-v1\"' }),
      new Response(null, { status: 304 }),
    );

    const first = await getApi("/api/news/feed", { etagKey: "feed" });
    const second = await getApi("/api/news/feed", { etagKey: "feed" });

    expect(requestHeaders(fetchMock, 0).has("if-none-match")).toBe(false);
    expect(requestHeaders(fetchMock, 1).get("if-none-match")).toBe('\"feed-v1\"');
    expect(first).toEqual(cached);
    expect(second).toBe(first);
  });

  it("isolates ETags and cached bodies by key", async () => {
    const fetchMock = stubFetch(
      jsonResponse({ ok: true, data: { id: "A" } }, 200, { etag: '\"a\"' }),
      jsonResponse({ ok: true, data: { id: "B" } }, 200, { etag: '\"b\"' }),
      new Response(null, { status: 304 }),
      new Response(null, { status: 304 }),
    );

    await getApi("/api/news/events/a", { etagKey: "event:a" });
    await getApi("/api/news/events/b", { etagKey: "event:b" });
    const a = await getApi<{ id: string }>("/api/news/events/a", { etagKey: "event:a" });
    const b = await getApi<{ id: string }>("/api/news/events/b", { etagKey: "event:b" });

    expect(requestHeaders(fetchMock, 2).get("if-none-match")).toBe('\"a\"');
    expect(requestHeaders(fetchMock, 3).get("if-none-match")).toBe('\"b\"');
    expect(a.data.id).toBe("A");
    expect(b.data.id).toBe("B");
  });

  it("clears conditional cache state when the authenticated session changes", async () => {
    const fetchMock = stubFetch(
      jsonResponse({ ok: true, data: { id: "old" } }, 200, { etag: '\"old\"' }),
      jsonResponse({ ok: true, data: { id: "fresh" } }),
    );
    setAuthToken("old-session");
    await getApi("/api/news/events/old", { etagKey: "event" });

    setAuthToken(null);
    await getApi("/api/news/events/old", { etagKey: "event" });

    expect(requestHeaders(fetchMock, 1).has("authorization")).toBe(false);
    expect(requestHeaders(fetchMock, 1).has("if-none-match")).toBe(false);
  });
});

type FetchMock = ReturnType<typeof vi.fn<typeof fetch>>;

function stubFetch(...responses: Response[]): FetchMock {
  const fetchMock = vi.fn<typeof fetch>();
  for (const response of responses) {
    fetchMock.mockResolvedValueOnce(response);
  }
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function jsonResponse(body: unknown, status = 200, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", ...headers },
  });
}

function requestUrl(fetchMock: FetchMock, index: number): URL {
  const input = fetchMock.mock.calls[index]?.[0];
  if (input === undefined) throw new Error(`missing fetch call ${index}`);
  return new URL(String(input));
}

function requestHeaders(fetchMock: FetchMock, index: number): Headers {
  const init = fetchMock.mock.calls[index]?.[1];
  if (init === undefined) throw new Error(`missing fetch options ${index}`);
  return new Headers(init.headers);
}
