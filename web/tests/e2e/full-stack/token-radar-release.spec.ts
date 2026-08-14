import { expect, test } from "@playwright/test";

test("v5 material facts flow through authenticated HTTP into the investigation path", async ({
  page,
}) => {
  const radarResponsePromise = page.waitForResponse(
    (response) => new URL(response.url()).pathname === "/api/token-radar",
  );
  await page.goto("/");
  const radarResponse = await radarResponsePromise;
  expect(radarResponse.status()).toBe(200);
  expect(radarResponse.request().headers()["authorization"]).toMatch(/^Bearer /);
  expect(radarResponse.headers()["etag"]).toMatch(/^"[0-9a-f]{64}"$/);
  const packet = (await radarResponse.json()) as { data: Record<string, unknown> };
  expect(Object.keys(packet.data).sort()).toEqual(
    ["eligible_total", "items", "schema_version", "social_evidence_as_of_ms"].sort(),
  );
  expect(packet.data.schema_version).toBe("token_radar_snapshot_v5");

  const item = page.locator(".live-radar-item").filter({ hasText: "$E2ERADAR" });
  await expect(item).toHaveCount(1);
  await expect(item.getByRole("group", { name: /Price \$12\.00, Observed/ })).toBeVisible();
  await expect(item.getByRole("group", { name: "Since signal +20%" })).toBeVisible();
  await expect(item.getByRole("group", { name: /Market cap \$12M, Observed/ })).toBeVisible();

  const caseLink = item.getByRole("link", { name: "Open E2ERADAR Token Case" });
  await expect(caseLink).toHaveAttribute(
    "href",
    "/token/Asset/e2e-radar-asset?window=4h&focus=trigger&trigger_event_id=e2e-radar-event-3",
  );
  await expect(item.getByRole("link", { name: "Open E2ERADAR on GMGN" })).toHaveAttribute(
    "target",
    "_blank",
  );
  await item.getByRole("button", { name: "Copy E2ERADAR contract address" }).click();
  await expect(page).toHaveURL(/\/$/);

  await caseLink.click({ position: { x: 24, y: 24 } });
  await expect(page).toHaveURL(
    /\/token\/Asset\/e2e-radar-asset\?window=4h&focus=trigger&trigger_event_id=e2e-radar-event-3$/,
  );
  await expect(page.getByRole("heading", { name: "E2E Radar" })).toBeVisible();
  await expect(page.getByRole("link", { name: "返回 Token Radar" })).toBeVisible();
  await page.getByRole("link", { name: "返回 Token Radar" }).click();
  await expect(page).toHaveURL(/\/$/);

  await page.goto(
    "/token/Asset/e2e-radar-asset?window=4h&focus=trigger&trigger_event_id=e2e-radar-event-3",
  );
  await page.getByRole("link", { name: "返回 Token Radar" }).click();
  await expect(page).toHaveURL(/\/$/);
  await expect
    .poll(() => page.locator(".live-radar-items").evaluate((element) => element.scrollTop))
    .toBe(0);

  const horizontalOverflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  expect(horizontalOverflow).toBeLessThanOrEqual(1);
});
