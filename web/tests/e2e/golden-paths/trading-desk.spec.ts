import { expect, test } from "@tests/e2e/fixtures";
import {
  expectNoDocumentHorizontalOverflow,
  expectNoUnhandledApiRequests,
} from "@tests/e2e/support/layoutAssertions";
import { installMockApi } from "@tests/e2e/support/mockApi";

/**
 * The desk's first browser coverage. `/trading` had none: twelve golden-path specs and not one of them
 * visited the route an operator uses to move capital (#604 T4).
 *
 * Four things have to hold at every width or the rebuild is worse than what it replaced. The safety strip
 * has to be the first thing on screen — it is the block that says whether the rest of the page is even
 * about a live account. The ledger has to scroll inside its own panel and never widen the document,
 * because a desk that pushes the viewport sideways hides the controls at the bottom of it. A Signal row
 * has to open its Case in the drawer and put that Case in the URL, because the URL is the only way an
 * operator can hand a frozen judgement to someone else. And the three writes have to stay reachable, at a
 * size a thumb can hit, on the device an operator actually has on them when something is wrong.
 *
 * No PNG snapshots: this repository has none and `updateSnapshots: "none"` is why. The assertions are
 * interaction and layout, which is what a rebuilt page can actually regress.
 */
test("cold-loads the desk, opens a Case from the ledger, and keeps the writes reachable", async ({
  page,
}, testInfo) => {
  const mobile = testInfo.project.name.startsWith("mobile-");
  await installMockApi(page);
  await page.goto("/trading");

  // ① The safety strip answers first, from `/api/trading/status` alone.
  await expect(page.getByRole("heading", { name: "Trading Desk" })).toBeVisible();
  const safety = page.getByLabel("执行安全状态");
  await expect(safety).toBeVisible();
  await expect(safety.getByText("ALIVE")).toBeVisible();
  await expect(safety.getByText("ARMED")).toBeVisible();
  // The fourth word went: `account_flat` is false with zero positions, so `FLAT` read NOT PROVEN forever.
  await expect(safety.getByText("FLAT")).toHaveCount(0);

  // ② and ③: the server's own realized totals, and the funnel from frame to closed position.
  await expect(page.getByRole("heading", { name: "今日战况" })).toBeVisible();
  const funnel = page.getByLabel("24h 漏斗");
  await expect(funnel).toBeVisible();
  await expect(funnel.getByText("帧", { exact: true })).toBeVisible();
  await expect(funnel.getByText("平仓", { exact: true })).toBeVisible();

  // ④ The ledger carries its own overflow. The document must not gain a horizontal scrollbar for it.
  const rows = page.locator(".trading-ledger-row");
  await expect(rows.first()).toBeVisible();
  await expect(rows).toHaveCount(4);
  const ledgerOverflow = await page
    .locator(".trading-ledger-table")
    .evaluate((element) => element.scrollWidth - element.clientWidth);
  expect(ledgerOverflow, "the ledger scrolls inside its own panel").toBeGreaterThanOrEqual(0);
  await expectNoDocumentHorizontalOverflow(page);

  // A Signal row opens its Case in the drawer, and the URL is what carries it.
  const signal = page.getByRole("button", { name: "crypto:perp:BTC:USDT" });
  await signal.click();
  await expect(page).toHaveURL(/\?case=case-btc$/);
  const drawer = page.getByLabel("案例抽屉");
  await expect(drawer).toBeVisible();
  await expect(page.getByRole("region", { name: /^案例 / })).toBeVisible();
  await expect(page.getByRole("heading", { name: "冻结策略配置" })).toHaveCount(0);
  await expectNoDocumentHorizontalOverflow(page);

  await drawer.getByRole("button", { name: "关闭" }).click();
  await expect(drawer).toHaveCount(0);
  await expect(page).toHaveURL(/\/trading$/);

  // ⑥ The three writes, at a size a thumb can find on the device an operator has on them.
  for (const name of ["Pause entries", "Resume / Arm", "Flatten account"]) {
    const control = page.getByRole("button", { name });
    await expect(control).toBeVisible();
    if (mobile) {
      const box = await control.boundingBox();
      expect(box?.height ?? 0, `${name} must be thumb-sized`).toBeGreaterThanOrEqual(48);
    }
  }

  /*
   * The phone reads the desk in a different order: is it alive, do I have exposure, do I need to press
   * something, then the three blocks that are only reading. On a desktop the column is the DOM order.
   */
  const blocks = await page.locator("[data-block]").evaluateAll((elements) =>
    elements
      .map((element) => ({
        name: element.getAttribute("data-block") ?? "",
        top: element.getBoundingClientRect().top,
      }))
      .sort((a, b) => a.top - b.top)
      .map((entry) => entry.name),
  );
  expect(blocks).toEqual(
    mobile
      ? ["safety", "exposure", "controls", "tally", "ledger", "funnel"]
      : ["safety", "tally", "funnel", "ledger", "exposure", "controls"],
  );

  await expectNoDocumentHorizontalOverflow(page);
  await expectNoUnhandledApiRequests(page);
});
