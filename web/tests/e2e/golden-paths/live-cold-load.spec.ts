import { expect, test } from "@playwright/test";
import {
  expectNoDocumentHorizontalOverflow,
  expectNoNestedHorizontalOverflow,
  expectNoUnhandledApiRequests,
  expectScrollableToLastMeaningfulElement,
} from "@tests/e2e/support/layoutAssertions";
import { installMockApi } from "@tests/e2e/support/mockApi";

test("cold live load renders one full-height Radar with local content age", async ({ page }) => {
  await installMockApi(page);
  await page.goto("/");
  const viewport = page.viewportSize();
  expect(viewport).not.toBeNull();

  const radarRow = page.getByRole("article", { name: "Token Radar item $UPEG" });
  await expect(radarRow).toBeVisible();
  await expect(radarRow.getByRole("link", { name: "Open token item $UPEG" })).toBeVisible();
  await expect(radarRow.getByText("4 帖 · 3 作者")).toBeVisible();
  const propagation = radarRow.locator('[data-case-section="propagation"]');
  await expect(propagation).toContainText("72 / 100");
  await expect(propagation).toContainText("4 informative · 0% duplicate");
  await expect(radarRow.locator(".market-move.up", { hasText: "+12%" })).toBeVisible();
  await expect(radarRow.locator('[data-radar-metric="market"]')).toContainText("liq$250K");
  await expect(radarRow.locator('[data-radar-metric="market"]')).toContainText("vol$250K");
  await expect(radarRow.locator('[data-radar-metric="market"]')).toContainText("holders1K");
  await expect(radarRow.getByRole("link", { name: "GMGN" })).toBeVisible();
  await expect(radarRow.getByText("profile")).toHaveCount(0);
  await expect(radarRow.getByText("unverified")).toHaveCount(0);
  await expect(page.getByRole("button", { name: /sort by holders/i })).toHaveCount(0);
  if ((viewport?.width ?? 0) >= 768) {
    await expect(page.getByRole("button", { name: /sort by market/i })).toBeVisible();
  } else {
    await expect(page.getByRole("button", { name: /sort by market/i })).toHaveCount(0);
  }
  const radarWindowControls = page.getByLabel("radar window");
  await expect(radarWindowControls.getByRole("radio", { name: "1h" })).toHaveAttribute(
    "data-state",
    "on",
  );
  await expect(page.getByLabel("token flow scope")).toHaveCount(0);
  await expect(page).toHaveURL(/\/$/);

  const shellBox = await page.locator(".cockpit-shell").boundingBox();
  expect(shellBox).not.toBeNull();
  expect(Math.round(shellBox?.x ?? -1)).toBe(0);
  expect(Math.round(shellBox?.y ?? -1)).toBe(0);
  expect(Math.round(shellBox?.width ?? 0)).toBe(viewport?.width);
  expect(Math.round(shellBox?.height ?? 0)).toBe(viewport?.height);

  const rowBox = await radarRow.boundingBox();
  expect(rowBox).not.toBeNull();
  if ((viewport?.width ?? 0) >= 768) {
    expect(Math.round(rowBox?.height ?? 0)).toBeLessThanOrEqual(72);
    expect(Math.round(rowBox?.height ?? 0)).toBeGreaterThanOrEqual(56);
  } else {
    expect(Math.round(rowBox?.height ?? 0)).toBeGreaterThanOrEqual(56);
  }

  if ((viewport?.width ?? 0) >= 1_000) {
    const scoreHeaderBox = await page.locator(".radar-head-cell.score").boundingBox();
    const scoreCellBox = await radarRow.locator(".radar-score-cell").boundingBox();
    const listedHeaderBox = await page.locator(".radar-head-cell.listed").boundingBox();
    const listedActionBox = await radarRow.locator(".radar-listed-action-cell").boundingBox();
    expect(scoreHeaderBox).not.toBeNull();
    expect(scoreCellBox).not.toBeNull();
    expect(listedHeaderBox).not.toBeNull();
    expect(listedActionBox).not.toBeNull();
    expect(
      Math.abs(
        Math.round((scoreHeaderBox?.x ?? 0) + (scoreHeaderBox?.width ?? 0)) -
          Math.round((scoreCellBox?.x ?? 0) + (scoreCellBox?.width ?? 0)),
      ),
    ).toBeLessThanOrEqual(2);
    expect(Math.round(scoreCellBox?.x ?? 0)).toBeLessThan(Math.round(listedActionBox?.x ?? 0));
    expect(
      Math.abs(
        Math.round((listedHeaderBox?.x ?? 0) + (listedHeaderBox?.width ?? 0)) -
          Math.round((listedActionBox?.x ?? 0) + (listedActionBox?.width ?? 0)),
      ),
    ).toBeLessThanOrEqual(2);
  }

  const primaryBox = await page.locator(".radar-toolbar-primary").boundingBox();
  const controlsBox = await page.getByLabel("token radar scan controls").boundingBox();
  const titleBox = await page.locator(".radar-scan-title").boundingBox();
  const statusBox = await page.getByTestId("radar-content-status").boundingBox();
  expect(primaryBox).not.toBeNull();
  expect(controlsBox).not.toBeNull();
  expect(titleBox).not.toBeNull();
  expect(statusBox).not.toBeNull();
  expect(statusBox!.x).toBeGreaterThanOrEqual(titleBox!.x + titleBox!.width);
  expect(statusBox!.x - (titleBox!.x + titleBox!.width)).toBeLessThanOrEqual(20);
  if ((viewport?.width ?? 0) > 860) {
    const toolbarBox = await page.locator(".radar-toolbar").boundingBox();
    expect(toolbarBox).not.toBeNull();
    expect(
      Math.abs(primaryBox!.y + primaryBox!.height / 2 - (controlsBox!.y + controlsBox!.height / 2)),
    ).toBeLessThanOrEqual(2);
    expect(primaryBox!.x + primaryBox!.width).toBeLessThanOrEqual(controlsBox!.x);
    expect(toolbarBox!.height).toBeLessThanOrEqual(64);
  } else {
    expect(primaryBox!.y + primaryBox!.height).toBeLessThanOrEqual(controlsBox!.y + 1);
  }
  await expect(page.getByTestId("radar-content-status")).toHaveAttribute("data-health", "healthy");
  await expect(page.getByTestId("radar-content-status")).toContainText(/最新内容 \d/);
  await expect(page.locator(".detail-task-panel")).toHaveCount(0);
  await expect(page.locator(".detail-drawer")).toHaveCount(0);
  await expect(page.getByText(/实时信号 Tape/i)).toHaveCount(0);
  await expect(page.locator(".live-task-nav")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Lab" })).toHaveCount(0);
  await expectNoDocumentHorizontalOverflow(page);
  await expectNoNestedHorizontalOverflow(page, [".topbar", ".radar-toolbar", ".token-radar-row"]);
  await expectNoUnhandledApiRequests(page);
});

test("radar row click reaches token detail without hit-test interception", async ({ page }) => {
  await installMockApi(page);
  await page.goto("/");

  const radarRow = page.getByRole("article", { name: "Token Radar item $UPEG" });
  await expect(radarRow).toBeVisible();

  const hitTest = await radarRow.evaluate((row) => {
    const rect = row.getBoundingClientRect();
    const element = document.elementFromPoint(
      rect.left + rect.width / 2,
      rect.top + rect.height / 2,
    );
    return {
      rowContainsHit: element ? row.contains(element) : false,
      hitClassName: element instanceof HTMLElement ? element.className : "",
      hitTagName: element?.tagName ?? "",
    };
  });
  expect(hitTest).toMatchObject({ rowContainsHit: true });

  await radarRow.click();
  await expect(page).toHaveURL(/\/token\/Asset\/asset%3Adex%3Aeth%3A/);
  await expectNoUnhandledApiRequests(page);
});

test("full-height Radar keeps the final row reachable without a task bar", async ({ page }) => {
  await installMockApi(page);
  await page.goto("/?window=24h");

  await expect(page.locator(".token-radar-row")).toHaveCount(8);
  await expect(page.locator(".live-task-nav")).toHaveCount(0);
  await expectScrollableToLastMeaningfulElement(
    page,
    ".token-radar-table",
    ".token-radar-row:last-of-type",
  );
  await expectNoDocumentHorizontalOverflow(page);
  await expectNoUnhandledApiRequests(page);
});
