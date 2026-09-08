import { expect, test } from "@tests/e2e/fixtures";
import { expectNoDocumentHorizontalOverflow } from "@tests/e2e/support/layoutAssertions";
import { installMockApi } from "@tests/e2e/support/mockApi";
import { newsWalletBuyFixture } from "@tests/fixtures/newsFixture";

test.beforeEach(async ({ page }) => {
  await installMockApi(page);
});

test("buy research separates cumulative amount, observed reference and measured outcomes", async ({
  page,
}) => {
  await page.goto("/news/wallets");
  const row = page.locator(".news-wallet-research-row").first();
  await expect(row).toContainText("观察期首次买入");
  await row.getByRole("button").click();
  await expect(
    page.getByText("观察后价格变化不代表钱包盈亏或跟单收益。", { exact: false }),
  ).toBeVisible();
  await expect(page.getByText("已计价成交均价", { exact: true })).toBeVisible();
  await expect(page.getByText("观察价 · USD / token", { exact: true })).toBeVisible();
  await expect(page.getByLabel("观察后价格")).toContainText("-20.00%");
  await expectNoDocumentHorizontalOverflow(page);
});

test("wallet and token filters survive window changes, reload and raw-detail return", async ({
  page,
}) => {
  const buy = newsWalletBuyFixture();
  await page.goto("/news/wallets");
  await page.locator(".news-wallet-research-row").first().getByRole("button").click();
  await page.getByRole("link", { name: "同钱包与代币的买卖 / 转出" }).click();
  await page.getByRole("button", { name: "7d", exact: true }).click();
  await page.reload();
  await expect(page.getByRole("textbox", { name: "钱包地址" })).toHaveValue(buy.wallet);
  await expect(page.getByRole("textbox", { name: "代币合约" })).toHaveValue(buy.token);
  expect(new URL(page.url()).searchParams.get("chain_id")).toBe(String(buy.chain_id));
  const fills = page.getByRole("region", { name: "交易流水" });
  await expect(fills.getByText("卖出", { exact: true })).toBeAttached();
  await expect(fills.getByText("转出", { exact: true })).toBeAttached();
  await expect(fills.getByText("1.234567890123456789").first()).toBeAttached();
  await page.getByRole("button", { name: "买入", exact: true }).click();
  await expect(fills.getByText("卖出", { exact: true })).toBeAttached();
  await page.getByRole("button", { name: "全部观察", exact: true }).click();
  await page.getByRole("button", { name: "清除筛选", exact: true }).click();
  expect(new URL(page.url()).searchParams.get("kind")).toBe("all");
  expect(new URL(page.url()).searchParams.has("wallet_address")).toBe(false);
  await expectNoDocumentHorizontalOverflow(page);
});
