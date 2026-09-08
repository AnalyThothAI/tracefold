import { allowBrowserFailure, expect, test } from "@tests/e2e/fixtures";
import { installMockApi } from "@tests/e2e/support/mockApi";
import { tradingLiveExecutionFixture, tradingStatusFixture } from "@tests/fixtures/tradingFixture";

test("live status stays current across polling windows, expires on lost reads, and recovers", async ({
  page,
}) => {
  await installMockApi(page);
  await page.clock.install();
  let unavailable = false;
  await page.route("**/api/trading/status", async (route) => {
    if (unavailable) {
      await route.fulfill({
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({ ok: false, error: "status_unavailable" }),
      });
      return;
    }
    const now = await page.evaluate(() => Date.now());
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        ok: true,
        data: tradingStatusFixture({
          execution: tradingLiveExecutionFixture({
            entries_armed: true,
            entries_paused: false,
            entry_block_reason: null,
            facts_expire_at_ms: now + 5_000,
          }),
        }),
      }),
    });
  });
  await page.goto("/trading");
  const safety = page.getByLabel("执行安全状态");
  await expect(safety.getByText("是", { exact: true })).toHaveCount(3);
  // Observe every second, including the gap between the former 15 s polls.
  for (let second = 0; second < 35; second += 1) {
    await page.clock.runFor(1_000);
    await expect(safety.getByText("是", { exact: true })).toHaveCount(3);
  }
  allowBrowserFailure(page, {
    kind: "console.error",
    match: /503 \(Service Unavailable\)/,
    reason: "This case deliberately interrupts the status API to verify fact expiry.",
  });
  unavailable = true;
  await page.clock.runFor(6_000);
  await expect(safety.getByText("是", { exact: true })).toHaveCount(0);
  await expect(page.getByText(/状态待确认：未取得有效期内的新状态/)).toBeVisible();
  await expect(page.getByText("全部覆盖", { exact: true })).toHaveCount(0);
  await expect(page.getByText("保护事实已过期", { exact: true })).toBeVisible();
  unavailable = false;
  await page.clock.runFor(1_000);
  await expect(safety.getByText("是", { exact: true })).toHaveCount(3);
  await expect(page.getByText(/状态待确认：未取得有效期内的新状态/)).toHaveCount(0);
});
