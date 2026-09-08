import { expect, test } from "@tests/e2e/fixtures";
import { expectNoDocumentHorizontalOverflow } from "@tests/e2e/support/layoutAssertions";
import { installMockApi } from "@tests/e2e/support/mockApi";
import { newsWalletBuyFixture } from "@tests/fixtures/newsFixture";

test.beforeEach(async ({ page }) => {
  await installMockApi(page);
});

test("buy research keeps unsent observations and distinct price bases readable", async ({
  page,
}) => {
  await page.goto("/news/wallets");
  const candidates = page.getByRole("region", { name: "钱包卡片" });
  await expect(candidates.getByRole("button", { name: "买入", exact: true })).toHaveAttribute(
    "aria-pressed",
    "true",
  );
  await expect(candidates.getByText("观察期首次买入")).toBeVisible();
  await expect(candidates.getByText("记录原因：buy_below_min_usd")).toBeVisible();
  await expect(candidates.getByText("价格基准：observed")).toBeVisible();
  await expect(candidates.getByRole("columnheader", { name: "已计价均价" })).toBeAttached();
  await expect(candidates.getByRole("columnheader", { name: "观察价" })).toBeAttached();
  await expect(candidates.getByText("-20.00%")).toBeAttached();
  await expect(candidates.getByRole("link", { name: "减仓", exact: true })).toHaveCount(0);
  await expectNoDocumentHorizontalOverflow(page);
});

test("wallet and token timeline filters survive window changes and a reload", async ({ page }) => {
  const buy = newsWalletBuyFixture();
  await page.goto("/news/wallets");
  await page.getByRole("link", { name: "同钱包与代币的后续变化" }).click();
  await expect(page).toHaveURL(
    new RegExp(`kind=all&wallet_address=${buy.wallet}&token_address=${buy.token}`),
  );
  await page.getByRole("button", { name: "7d", exact: true }).click();
  await expect(page).toHaveURL(
    new RegExp(`window=7d&kind=all&wallet_address=${buy.wallet}&token_address=${buy.token}`),
  );
  await page.reload();
  await expect(page.getByRole("textbox", { name: "钱包地址" })).toHaveValue(buy.wallet);
  await expect(page.getByRole("textbox", { name: "代币合约" })).toHaveValue(buy.token);
  await expect(page.getByRole("button", { name: "全部", exact: true })).toHaveAttribute(
    "aria-pressed",
    "true",
  );
  await expect(page.getByRole("link", { name: "减仓", exact: true })).toBeAttached();
  const fills = page.getByRole("region", { name: "交易流水" });
  await expect(fills.getByText("卖出", { exact: true })).toBeAttached();
  await expect(fills.getByText("转出", { exact: true })).toBeAttached();
  await expect(fills.getByText("1.234567890123456789").first()).toBeAttached();
  await page.getByRole("button", { name: "买入", exact: true }).click();
  await expect(page.getByRole("link", { name: "减仓", exact: true })).toHaveCount(0);
  await expect(fills.getByText("卖出", { exact: true })).toBeAttached();
  await expect(fills.getByText("转出", { exact: true })).toBeAttached();
  await page.getByRole("button", { name: "全部", exact: true }).click();
  await page.getByRole("button", { name: "清除地址", exact: true }).click();
  await expect(page).toHaveURL(/\/news\/wallets\?window=7d&kind=all$/);
  await expectNoDocumentHorizontalOverflow(page);
});
