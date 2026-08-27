import { expect, test } from "@tests/e2e/fixtures";

test("FastAPI serves the console, installs bootstrap bearer, and renders one News fact", async ({
  page,
}) => {
  const bootstrapResponsePromise = page.waitForResponse(isBootstrapResponse);
  const feedRequestPromise = page.waitForRequest(isFeedRequest);
  const feedResponsePromise = page.waitForResponse(isFeedResponse);

  const documentResponse = await page.goto("/news");
  expect(documentResponse?.ok()).toBe(true);
  expect(documentResponse?.headers()["content-type"]).toContain("text/html");

  const bootstrapResponse = await bootstrapResponsePromise;
  expect(bootstrapResponse.ok()).toBe(true);
  const bootstrap = asObject(await bootstrapResponse.json());
  const bootstrapData = asObject(bootstrap.data);
  const token = bootstrapData.ws_token;
  expect(typeof token === "string" && token.length > 0).toBe(true);

  const feedRequest = await feedRequestPromise;
  const authorization = feedRequest.headers()["authorization"];
  expect(
    typeof token === "string" && authorization === `Bearer ${token}`,
    "The News read must carry the opaque token returned by bootstrap.",
  ).toBe(true);

  const feedResponse = await feedResponsePromise;
  expect(feedResponse.ok()).toBe(true);
  expect(new URL(feedResponse.url()).origin).toBe(new URL(documentResponse?.url() ?? "").origin);
  const feed = asObject(await feedResponse.json());
  const feedData = asObject(feed.data);
  const events = feedData.events;
  expect(Array.isArray(events) && events.length > 0, "The smoke fixture must seed one Event.").toBe(
    true,
  );
  const firstEvent = asObject(Array.isArray(events) ? events[0] : null);
  const triage = asObject(firstEvent.triage);
  const headline = firstNonEmptyString(
    triage.headline_zh,
    firstEvent.title_zh,
    firstEvent.leader_title,
  );
  expect(headline !== null, "The seeded Event must expose a reader headline.").toBe(true);

  await expect(page.getByRole("heading", { name: "新闻事件流" })).toBeVisible();
  await expect(page.getByRole("heading", { name: headline ?? "__missing__" })).toBeVisible();
});

function isBootstrapResponse(response: import("@playwright/test").Response): boolean {
  return (
    response.request().method() === "GET" && new URL(response.url()).pathname === "/api/bootstrap"
  );
}

function isFeedRequest(request: import("@playwright/test").Request): boolean {
  return request.method() === "GET" && new URL(request.url()).pathname === "/api/news/feed";
}

function isFeedResponse(response: import("@playwright/test").Response): boolean {
  return isFeedRequest(response.request());
}

function asObject(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function firstNonEmptyString(...values: unknown[]): string | null {
  for (const value of values) {
    if (typeof value === "string" && value.trim() !== "") return value;
  }
  return null;
}
