import { expect, test } from "@playwright/test";
import { installMockApi } from "@tests/e2e/support/mockApi";

const targetId = "asset:solana:token:FhoxjfsuStvRQKRXSuB9ZDB7WRGjqhUPxa3NztWspump";

test("token route renders the HANSA case dossier and loads another post page", async ({ page }) => {
  await installMockApi(page);
  await page.goto(`/token/Asset/${encodeURIComponent(targetId)}?window=1h`);

  const tokenCase = page.getByRole("region", { name: "Token case" });
  await expect(tokenCase.getByRole("heading", { name: /\$HANSA/ })).toBeVisible();
  await expect(tokenCase.getByRole("heading", { name: "Mention Timeline" })).toBeVisible();
  await expect(tokenCase.getByRole("heading", { name: "Live Market" })).toBeVisible();
  await expect(
    tokenCase.getByRole("article").filter({ hasText: "Expansion leg forming on $HANSA" }),
  ).toBeVisible();

  await tokenCase.getByRole("button", { name: "Load more" }).click();
  await expect(
    tokenCase.getByRole("article").filter({ hasText: "Follow-up page adds fresh HANSA context" }),
  ).toBeVisible();

  const routeBackSentinel = await page.evaluate(() => {
    const routeWindow = window as Window & { __routeBackSentinel?: string };
    routeWindow.__routeBackSentinel = crypto.randomUUID();
    return routeWindow.__routeBackSentinel;
  });

  await tokenCase.getByRole("link", { name: "返回 Token Radar" }).click();

  await expect(page.getByRole("heading", { name: "Radar" })).toBeVisible();
  await expect
    .poll(() =>
      page.evaluate(
        () => (window as Window & { __routeBackSentinel?: string }).__routeBackSentinel,
      ),
    )
    .toBe(routeBackSentinel);
});

test("search token_result reuses the case dossier without fetching token-case", async ({
  page,
}) => {
  const tokenCaseRequests: string[] = [];
  await installMockApi(page);
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.pathname === "/api/token-case") tokenCaseRequests.push(request.url());
  });

  await page.goto("/search?q=HANSA&window=24h");

  const tokenCase = page.getByRole("region", { name: "Token case" });
  await expect(tokenCase.getByRole("heading", { name: /\$HANSA/ })).toBeVisible();
  await expect(tokenCase.getByRole("heading", { name: "Mention Timeline" })).toBeVisible();
  await expect(tokenCase.getByRole("button", { name: "Load more" })).toHaveCount(0);
  expect(tokenCaseRequests).toEqual([]);
});
