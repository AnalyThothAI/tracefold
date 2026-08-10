import { expect, test } from "@playwright/test";
import {
  expectNoDocumentHorizontalOverflow,
  expectNoNestedHorizontalOverflow,
  expectNoUnhandledApiRequests,
  expectScrollableToLastMeaningfulElement,
} from "@tests/e2e/support/layoutAssertions";
import { installMockApi } from "@tests/e2e/support/mockApi";

test("cold Radar load renders the rich change-first queue", async ({ page }) => {
  await installMockApi(page);
  const radarRequests: string[] = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.pathname === "/api/token-radar") radarRequests.push(url.search);
  });
  await page.goto("/");

  await expect(page.getByRole("heading", { name: "Radar" })).toBeVisible();
  await expect(page.getByText("1 eligible")).toBeVisible();
  const item = page.locator(".live-radar-item").first();
  await expect(item.getByText("$UPEG", { exact: true })).toBeVisible();
  await expect(item.getByRole("img", { name: "Unpegged Token icon" })).toBeVisible();
  await expect(item.getByRole("group", { name: "Price $0.042" })).toBeVisible();
  await expect(item.getByRole("group", { name: "Since signal +12%" })).toBeVisible();
  await expect(item.getByRole("group", { name: "Market cap $42M" })).toBeVisible();
  await expect(item.getByRole("group", { name: "Attention +5, 2 to 7 mentions" })).toBeVisible();
  await expect(
    item.getByRole("group", { name: "Independent evidence, 4 authors, 5 texts" }),
  ).toBeVisible();
  await expect(item.locator("details")).toHaveCount(0);
  await expect(item.getByRole("button", { name: "Copy UPEG contract address" })).toBeVisible();
  await expect(item.getByRole("link", { name: "Open UPEG on GMGN" })).toHaveAttribute(
    "href",
    "https://gmgn.ai/eth/token/0x6982508145454Ce325dDbE47a25d4ec3d2311933",
  );
  await expect(page.getByLabel("radar window")).toHaveCount(0);
  await expect(page.getByLabel("token radar venue filter")).toHaveCount(0);
  await expect(page.getByRole("button", { name: /sort/i })).toHaveCount(0);
  await expect(item.getByRole("link", { name: "Open Token Case" })).toHaveAttribute(
    "href",
    /\?window=1h&focus=trigger&trigger_event_id=event-upeg-1$/,
  );
  expect(radarRequests).toEqual([""]);
  expect(await page.locator(".live-radar-queue *").count()).toBeLessThanOrEqual(1_100);
  await expectNoDocumentHorizontalOverflow(page);
  await expectNoNestedHorizontalOverflow(page, [".topbar", ".live-radar-item"]);
  await expectNoUnhandledApiRequests(page);
});

test("Radar's single action opens a Case focused on the trigger", async ({ page }) => {
  await installMockApi(page);
  await page.goto("/");

  const item = page.locator(".live-radar-item").first();
  await item.getByRole("link", { name: "Open Token Case" }).click();

  await expect(page).toHaveURL(
    /\/token\/Asset\/asset%3Adex%3Aeth%3A.*\?window=1h&focus=trigger&trigger_event_id=event-upeg-1$/,
  );
  await expect(page.getByRole("article", { name: "Trigger evidence" })).toBeVisible();
  await expectNoUnhandledApiRequests(page);
});

test("the capped fifty-item Radar keeps its final item reachable", async ({ page }) => {
  await installMockApi(page, { radarItemCount: 50 });
  await page.goto("/");

  await expect(page.locator(".live-radar-item")).toHaveCount(50);
  await expectScrollableToLastMeaningfulElement(
    page,
    ".live-radar-items",
    ".live-radar-item:last-of-type",
  );
  expect(await page.locator(".live-radar-queue *").count()).toBeLessThanOrEqual(1_100);
  await expectNoDocumentHorizontalOverflow(page);
  await expectNoUnhandledApiRequests(page);
});

test("a full snapshot refresh stays below the 50ms browser long-task gate", async ({ page }) => {
  await page.clock.install({ time: new Date(1_777_746_300_000) });
  await page.addInitScript(() => {
    const target = window as typeof window & { __radarLongTasks?: number[] };
    target.__radarLongTasks = [];
    new PerformanceObserver((entries) => {
      target.__radarLongTasks?.push(...entries.getEntries().map((entry) => entry.duration));
    }).observe({ type: "longtask", buffered: true });
  });
  await installMockApi(page, { radarItemCount: 0, radarRefreshItemCount: 50 });
  await page.goto("/");
  await expect(page.locator(".live-radar-item")).toHaveCount(1);
  await expect(page.getByText("No eligible cases")).toBeVisible();
  await page.evaluate(() => {
    (window as typeof window & { __radarLongTasks?: number[] }).__radarLongTasks = [];
  });

  await page.clock.fastForward(30_000);
  await expect(page.locator(".live-radar-item")).toHaveCount(50);
  await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => resolve(null))));

  const longTasks = await page.evaluate(
    () => (window as typeof window & { __radarLongTasks?: number[] }).__radarLongTasks ?? [],
  );
  expect(longTasks.filter((duration) => duration > 50)).toEqual([]);
  expect(await page.locator(".live-radar-queue *").count()).toBeLessThanOrEqual(1_100);
  await expectNoUnhandledApiRequests(page);
});
