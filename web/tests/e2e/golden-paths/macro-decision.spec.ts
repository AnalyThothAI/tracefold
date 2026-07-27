import { expect, test, type Page } from "@playwright/test";
import {
  expectNoDocumentHorizontalOverflow,
  expectNoNestedHorizontalOverflow,
  expectNoUnhandledApiRequests,
  expectScrollableToLastMeaningfulElement,
} from "@tests/e2e/support/layoutAssertions";
import { installMockApi } from "@tests/e2e/support/mockApi";

const modules = [
  ["/macro/rates-fed", "利率与美联储"],
  ["/macro/economy-inflation", "经济与通胀"],
  ["/macro/liquidity-funding", "流动性与融资"],
  ["/macro/credit", "信用市场"],
  ["/macro/volatility", "波动率"],
  ["/macro/cross-asset", "大类资产与期货"],
] as const;

test.beforeEach(async ({ page }) => {
  await installMockApi(page);
});

test("renders the daily judgment, six modules and asynchronous research", async ({ page }) => {
  await page.goto("/macro");

  await expect(page.getByRole("heading", { level: 1, name: "每日宏观决策台" })).toBeVisible();
  await expect(
    page.getByRole("navigation", { name: "宏观决策模块" }).getByRole("link"),
  ).toHaveCount(8);
  await expect(page.getByRole("region", { name: "六个宏观模块" })).toBeVisible();
  await expect(page.getByText("固定资产方向")).toBeVisible();
  await expect(page.getByText("mep_fixture")).toBeVisible();
  await expect(page.getByText("pass")).toBeVisible();
  await expect(page.getByRole("link", { name: /查看深度研究/ })).toHaveAttribute(
    "href",
    "/macro/research",
  );

  await expectMacroLayout(page);
  await expectNoUnhandledApiRequests(page);
});

for (const [path, title] of modules) {
  test(`hard-loads ${title} with typed facts and decision checkpoints`, async ({ page }) => {
    await page.goto(path);

    await expect(page.getByRole("heading", { level: 1, name: title })).toBeVisible();
    await expect(page.getByRole("heading", { level: 2, name: "矛盾与反证" })).toBeVisible();
    await expect(page.getByRole("heading", { level: 2, name: "判断失效条件" })).toBeVisible();
    await expect(page.getByRole("heading", { level: 2, name: "下一检查点" })).toBeVisible();
    await expect(page.getByText("展开原始证据与 Dataset 状态")).toBeVisible();
    await expect(page.getByRole("main", { name: title })).not.toContainText("历史窗口");

    await expectMacroLayout(page);
    await expectScrollableToLastMeaningfulElement(
      page,
      ".center-column",
      ".macro-decision__evidence",
    );
    await expectNoUnhandledApiRequests(page);
  });
}

async function expectMacroLayout(page: Page) {
  await expectNoDocumentHorizontalOverflow(page);
  await expectNoNestedHorizontalOverflow(page, [
    ".macro-decision",
    ".macro-decision__header",
    ".macro-decision__nav",
    ".macro-decision__module-grid",
  ]);
}
