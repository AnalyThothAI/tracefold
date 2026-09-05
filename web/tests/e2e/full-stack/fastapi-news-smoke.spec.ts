import { allowBrowserFailure, expect, test } from "@tests/e2e/fixtures";

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
  const feedFailure = feedResponse.ok()
    ? ""
    : `Initial News feed failed: status=${feedResponse.status()} url=${feedResponse.url()} body=${await feedResponse.text()}`;
  expect(feedResponse.ok(), feedFailure).toBe(true);
  expect(new URL(feedResponse.url()).origin).toBe(new URL(documentResponse?.url() ?? "").origin);
  const feed = asObject(await feedResponse.json());
  const feedData = asObject(feed.data);
  const events = feedData.events;
  expect(Array.isArray(events) && events.length > 0, "The smoke fixture must seed one Event.").toBe(
    true,
  );
  const firstEvent = asObject(Array.isArray(events) ? events[0] : null);
  const triage = asObject(firstEvent.triage);
  const headline = firstNonEmptyString(triage.headline_zh, firstEvent.leader_title);
  expect(headline !== null, "The seeded Event must expose a reader headline.").toBe(true);

  await expect(page.getByRole("heading", { name: "新闻事件流" })).toBeVisible();
  await expect(page.getByRole("heading", { name: headline ?? "__missing__" })).toBeVisible();

  // #336: this crosses the real topbar -> URL -> FastAPI -> PostgreSQL -> response-metadata seam.
  // Begin inside an intentionally narrow old task so the assertion proves a new search cannot inherit it.
  allowBrowserFailure(page, {
    kind: "requestfailed",
    match: /GET \/api\/news\/(?:feed|status) \(net::ERR_ABORTED\)/,
    reason: "Changing route scope intentionally supersedes the prior polling reads.",
  });
  await page.goto(
    "/news?symbol=NOPE&event_family=other&outcome=held&hours=1&direction=bullish&event_kind=listing",
  );
  const searchedFeedPromise = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return (
      isFeedResponse(response) &&
      url.searchParams.get("q") === "perpetual futures" &&
      url.searchParams.get("hours") === "168"
    );
  });
  const searchInput = page.getByLabel("news search");
  await expect(searchInput).toHaveAttribute("placeholder", "标的 / 事件关键词");
  await searchInput.fill("perpetual futures");
  await searchInput.press("Enter");

  const searchedUrl = new URL(page.url());
  expect(Array.from(searchedUrl.searchParams.keys())).toEqual(["q", "outcome", "hours"]);
  const searchedFeed = asObject(await (await searchedFeedPromise).json());
  const searchedData = asObject(searchedFeed.data);
  expect(searchedData.search).toEqual({
    mode: "text",
    normalized_query: "perpetual futures",
    resolved_symbols: [],
  });
  await expect(page.getByText("全文匹配：perpetual futures", { exact: true })).toBeVisible();
  await expect(page.getByText("全部结果 · 最近 7 天", { exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: headline ?? "__missing__" })).toBeVisible();
});

test("The market page renders the observation the broker delivered, parsed and raw alike", async ({
  page,
}) => {
  // #553. The market plane is not an Event, so nothing on the News feed proves it arrived. This is
  // the same frame the smoke published, read back through `/api/news/market` and rendered.
  const marketResponsePromise = page.waitForResponse(isMarketResponse);

  const documentResponse = await page.goto("/news/market");
  expect(documentResponse?.ok()).toBe(true);

  const marketResponse = await marketResponsePromise;
  const marketFailure = marketResponse.ok()
    ? ""
    : `Market read failed: status=${marketResponse.status()} body=${await marketResponse.text()}`;
  expect(marketResponse.ok(), marketFailure).toBe(true);
  const market = asObject(await marketResponse.json());
  const marketData = asObject(market.data);
  const groups = marketData.groups;
  expect(
    Array.isArray(groups) && groups.length > 0,
    "The smoke fixture must seed one market observation.",
  ).toBe(true);

  await expect(page.getByRole("heading", { name: "市场事实" })).toBeVisible();
  const liquidation = page.locator('.news-market-row[data-kind="liquidation"]');
  await expect(liquidation).toHaveCount(1);
  await expect(liquidation).toContainText("BTC Large Short Liquidation 4.55M at $118000");
  // Two independent answers on the same row: what the parser read, and what the sender did. PR-1
  // wires no notification loop and the page states that from the server's own flag.
  await expect(liquidation.locator('[data-flag="parse"][data-status="parsed"]')).toBeVisible();
  await expect(liquidation.locator('[data-flag="push"]')).toBeVisible();
  expect(marketData.notifications_connected).toBe(false);

  // The Event feed never carries it, which is the whole point of the cut.
  const feedResponse = await page.request.get("/api/news/feed?limit=100", {
    headers: { Authorization: `Bearer ${await bootstrapToken(page)}` },
  });
  expect(feedResponse.ok()).toBe(true);
  const feed = asObject(await feedResponse.json());
  const events = asObject(feed.data).events;
  expect(
    Array.isArray(events) &&
      events.every(
        (event) => asObject(event).leader_title !== "BTC Large Short Liquidation 4.55M at $118000",
      ),
    "A market observation must not appear on the Event feed.",
  ).toBe(true);
});

async function bootstrapToken(page: import("@playwright/test").Page): Promise<string> {
  const response = await page.request.get("/api/bootstrap");
  const bootstrap = asObject(await response.json());
  const token = asObject(bootstrap.data).ws_token;
  return typeof token === "string" ? token : "";
}

function isMarketResponse(response: import("@playwright/test").Response): boolean {
  return (
    response.request().method() === "GET" &&
    new URL(response.url()).pathname === "/api/news/market" &&
    response.status() !== 304
  );
}

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
