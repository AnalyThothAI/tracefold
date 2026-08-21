import { expect, test } from "@playwright/test";
import { expectNoUnhandledApiRequests } from "@tests/e2e/support/layoutAssertions";
import { installMockApi } from "@tests/e2e/support/mockApi";

/**
 * The Event drawer beside the list (design proposal ⑦), now that a click is the only way into it.
 *
 * This used to ride along inside the keyboard contract spec, which went with the keyboard layer. Four things
 * have to hold or the drawer is worse than a full page: the queue survives the open, clicking the next row
 * swaps the panel, clicking neutral chrome leaves it alone, and a live prepend does not move the Event the
 * reader is standing on.
 *
 * The neutral-chrome click is the one that pins `Drawer`'s `onInteractOutside` suppression. The row swap does
 * not: without the suppression Radix closes on the outside pointerdown and the row's own handler reopens it
 * on the same tick, so the swap survives either way and asserting it proves nothing about dismissal.
 */
test.beforeEach(({}, testInfo) => {
  test.skip(!testInfo.project.name.startsWith("desktop-"), "the drawer needs ≥1024px");
});

const openFullPage = (eventId: string) => `/news/events/${eventId}`;

test("opens beside the list, swaps to the next row, and closes on Esc", async ({ page }) => {
  await installMockApi(page);
  await page.goto("/news");
  const rows = page.locator(".news-event-row");
  await expect(rows.first()).toBeVisible();
  const rowCount = await rows.count();

  await page.locator('[data-event-id="evt-global-policy"] h2 a').click();
  const panel = page.getByRole("dialog");
  await expect(panel.getByRole("link", { name: "打开整页" })).toHaveAttribute(
    "href",
    openFullPage("evt-global-policy"),
  );
  // Non-modal: the queue is still on screen and the URL still belongs to the feed.
  await expect(rows).toHaveCount(rowCount);
  await expect(page).toHaveURL(/\/news(\?|$)/);
  // Radix portals the panel to the end of `body`, so opening has to move focus into it or a keyboard reader
  // would tab through every remaining row to reach 打开整页. Which control takes it is Radix's business.
  await expect(panel.locator(":focus")).toHaveCount(1);

  // A click outside the panel is how the reader moves through the queue, so it must swap, never dismiss.
  await page.locator('[data-event-id="evt-global-policy-3"] h2 a').click();
  await expect(panel.getByRole("link", { name: "打开整页" })).toHaveAttribute(
    "href",
    openFullPage("evt-global-policy-3"),
  );

  // Neutral chrome: nothing reopens the panel behind this click, so it is the one that proves the drawer
  // does not dismiss on an outside interaction.
  await page.locator(".news-funnel-card").click({ position: { x: 4, y: 4 } });
  await expect(panel).toHaveCount(1);
  await expect(panel.getByRole("link", { name: "打开整页" })).toHaveAttribute(
    "href",
    openFullPage("evt-global-policy-3"),
  );

  await page.keyboard.press("Escape");
  await expect(panel).toHaveCount(0);
  await expect(rows).toHaveCount(rowCount);
  await expect(page).toHaveURL(/\/news(\?|$)/);
  await expectNoUnhandledApiRequests(page);
});

test("keeps its Event when a newer one lands at the top of the feed", async ({ page }) => {
  const feed = await installMockApi(page);
  await page.goto("/news");
  await expect(page.locator(".news-event-row").first()).toBeVisible();

  await page.locator('[data-event-id="evt-global-policy-2"] h2 a').click();
  const openLink = page.getByRole("dialog").getByRole("link", { name: "打开整页" });
  await expect(openLink).toHaveAttribute("href", openFullPage("evt-global-policy-2"));

  // A live feed prepends and every row index shifts by one. The drawer holds an Event, not a position.
  feed.prependEvent("evt-breaking");
  await expect(page.locator(".news-event-row").first()).toHaveAttribute(
    "data-event-id",
    "evt-breaking",
  );
  await expect(openLink).toHaveAttribute("href", openFullPage("evt-global-policy-2"));
});
