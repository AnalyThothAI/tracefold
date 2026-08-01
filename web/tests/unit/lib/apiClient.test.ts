import { getApi } from "@lib/api/client";
import { afterEach, describe, expect, it, vi } from "vitest";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("API client errors", () => {
  it("preserves HTTP status when an error response is not JSON", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("Internal Server Error", { status: 500 })),
    );

    await expect(getApi("/api/macro/overview")).rejects.toMatchObject({
      name: "ApiError",
      message: "Internal Server Error",
      status: 500,
    });
  });
});
