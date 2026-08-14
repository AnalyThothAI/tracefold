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
  await expect(page.getByText("1 eligible · showing 1 / 50")).toBeVisible();
  await expect(page.getByText("4h causal change · newest qualification first")).toBeVisible();
  const item = page.locator(".live-radar-item").first();
  await expect(item.getByText("$UPEG", { exact: true })).toBeVisible();
  await expect(item.getByRole("img", { name: "Unpegged Token icon" })).toBeVisible();
  await expect(item.getByRole("group", { name: /Price \$0\.042, Observed/ })).toBeVisible();
  await expect(item.getByRole("group", { name: "Since signal +12%" })).toBeVisible();
  await expect(item.getByRole("group", { name: /Market cap \$42M, Observed/ })).toBeVisible();
  await expect(item.getByRole("group", { name: "Mentions 2 to 7, increase 5" })).toBeVisible();
  await expect(
    item.getByRole("group", {
      name: "Independent evidence, 4 independent authors, 5 independent texts",
    }),
  ).toBeVisible();
  await expect(item.getByTitle("Trigger source-event time")).toBeVisible();
  await expect(item.getByTitle("Qualification time")).toBeVisible();
  await expect(item.locator("details")).toHaveCount(0);
  await expect(item.getByRole("button", { name: "Copy UPEG contract address" })).toBeVisible();
  await expect(item.getByRole("link", { name: "Open UPEG on GMGN" })).toHaveAttribute(
    "href",
    "https://gmgn.ai/eth/token/0x6982508145454Ce325dDbE47a25d4ec3d2311933",
  );
  await expect(page.getByLabel("radar window")).toHaveCount(0);
  await expect(page.getByLabel("token radar venue filter")).toHaveCount(0);
  await expect(page.getByRole("button", { name: /sort/i })).toHaveCount(0);
  await expect(item.getByRole("link", { name: "Open UPEG Token Case" })).toHaveAttribute(
    "href",
    /\?window=4h&focus=trigger&trigger_event_id=event-upeg-1$/,
  );
  expect(radarRequests).toEqual([""]);
  await expectNoDocumentHorizontalOverflow(page);
  await expectNoNestedHorizontalOverflow(page, [".topbar", ".live-radar-item"]);
  await expectNoUnhandledApiRequests(page);

  const caseLink = item.getByRole("link", { name: "Open UPEG Token Case" });
  await caseLink.focus();
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(
    /\/token\/Asset\/asset%3Adex%3Aeth%3A.*\?window=4h&focus=trigger&trigger_event_id=event-upeg-1$/,
  );
});

test("card navigation keeps controls isolated and restores Radar scroll on explicit return", async ({
  page,
}) => {
  await installMockApi(page, { radarItemCount: 50 });
  await page.goto("/");

  const firstItem = page.locator(".live-radar-item").first();
  await firstItem.getByRole("button", { name: "Copy UPEG contract address" }).click();
  await expect(page).toHaveURL(/\/$/);
  await expect(firstItem.getByRole("link", { name: "Open UPEG on GMGN" })).toHaveAttribute(
    "target",
    "_blank",
  );

  const list = page.locator(".live-radar-items");
  const item = page.locator(".live-radar-item").nth(8);
  await list.evaluate((element, itemIndex) => {
    const target = element.children.item(itemIndex);
    if (!(target instanceof HTMLElement)) throw new Error("Radar target item is unavailable");
    element.scrollTop = Math.max(0, target.offsetTop - 16);
  }, 8);
  const scrollTop = await list.evaluate((element) => element.scrollTop);
  expect(scrollTop).toBeGreaterThan(0);
  await item
    .getByRole("link", { name: "Open CASE9 Token Case" })
    .click({ position: { x: 20, y: 20 } });

  await expect(page).toHaveURL(
    /\/token\/Asset\/asset%3Adex%3Aeth%3A.*%3A9\?window=4h&focus=trigger&trigger_event_id=event-upeg-9$/,
  );
  await page.getByRole("link", { name: "返回 Token Radar" }).click();
  await expect(page).toHaveURL(/\/$/);
  await expect.poll(() => list.evaluate((element) => element.scrollTop)).toBe(scrollTop);
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
  await expectNoDocumentHorizontalOverflow(page);
  await expectNoUnhandledApiRequests(page);
});
