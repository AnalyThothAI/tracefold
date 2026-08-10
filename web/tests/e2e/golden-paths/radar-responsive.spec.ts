// @responsive-spec
import { expect, test } from "@playwright/test";
import {
  expectNoDocumentHorizontalOverflow,
  expectNoNestedHorizontalOverflow,
} from "@tests/e2e/support/layoutAssertions";
import { installMockApi } from "@tests/e2e/support/mockApi";

test.beforeEach(({}, testInfo) => {
  test.skip(testInfo.project.name !== "desktop-1366", "fixed narrow-desktop visual contract");
});

test("Radar remains readable in the narrow desktop workbench", async ({ page }) => {
  await page.setViewportSize({ width: 1_280, height: 504 });
  await installMockApi(page, { radarItemCount: 8, radarPresentationStress: true });
  await page.goto("/");

  const item = page.locator(".live-radar-item").first();
  const identity = item.locator(".live-radar-identity");
  const market = item.locator(".live-radar-market");
  const evidence = item.locator(".live-radar-item-evidence");
  const action = item.getByRole("link", { name: "Open Token Case" });
  await expect(item).toBeVisible();

  const layout = await item.evaluate((element) => {
    const itemBox = element.getBoundingClientRect();
    const identityElement = element.querySelector<HTMLElement>(".live-radar-identity");
    const marketElement = element.querySelector<HTMLElement>(".live-radar-market");
    const evidenceElement = element.querySelector<HTMLElement>(".live-radar-item-evidence");
    const actionElement = element.querySelector<HTMLElement>("a");
    if (!identityElement || !marketElement || !evidenceElement || !actionElement) {
      throw new Error("Radar Item is missing a visible information group");
    }
    const identityBox = identityElement.getBoundingClientRect();
    const marketBox = marketElement.getBoundingClientRect();
    const evidenceBox = evidenceElement.getBoundingClientRect();
    const actionBox = actionElement.getBoundingClientRect();
    const symbolElement = identityElement.querySelector<HTMLElement>("strong");
    const nameElement = identityElement.querySelector<HTMLElement>(".live-radar-token-copy > span");
    if (!symbolElement || !nameElement) throw new Error("Radar identity hierarchy is incomplete");
    const evidenceFacts = [...evidenceElement.querySelectorAll<HTMLElement>("[role=group]")];
    const marketFacts = [...marketElement.querySelectorAll<HTMLElement>("[role=group]")];
    return {
      actionContained:
        actionBox.left >= itemBox.left &&
        actionBox.top >= itemBox.top &&
        actionBox.right <= itemBox.right &&
        actionBox.bottom <= itemBox.bottom &&
        itemBox.right - actionBox.right >= 16 &&
        actionBox.height >= 32,
      evidenceBelowPrimaryFacts:
        evidenceBox.top >= Math.max(identityBox.bottom, marketBox.bottom) + 4,
      evidenceFontReadable: evidenceFacts.every(
        (fact) =>
          Number.parseFloat(getComputedStyle(fact).fontSize) >= 12 &&
          Number.parseFloat(getComputedStyle(fact, "::before").fontSize) >= 11,
      ),
      evidenceFits: evidenceFacts.every(
        (fact) =>
          fact.scrollWidth <= fact.clientWidth + 1 && fact.scrollHeight <= fact.clientHeight + 1,
      ),
      fourRowsVisible:
        [...element.ownerDocument.querySelectorAll<HTMLElement>(".live-radar-item")].filter(
          (row) => {
            const box = row.getBoundingClientRect();
            return box.top >= 0 && box.bottom <= innerHeight;
          },
        ).length >= 4,
      identityHierarchy:
        Number.parseFloat(getComputedStyle(symbolElement).fontSize) >=
        Number.parseFloat(getComputedStyle(nameElement).fontSize) + 2,
      itemHeightReadable: itemBox.height >= 84 && itemBox.height <= 120,
      marketFactsFit: marketFacts.every(
        (fact) =>
          Number.parseFloat(getComputedStyle(fact).fontSize) >= 13 &&
          Number.parseFloat(getComputedStyle(fact, "::before").fontSize) >= 11 &&
          fact.scrollWidth <= fact.clientWidth + 1 &&
          fact.scrollHeight <= fact.clientHeight + 1,
      ),
    };
  });

  expect(layout).toEqual({
    actionContained: true,
    evidenceBelowPrimaryFacts: true,
    evidenceFontReadable: true,
    evidenceFits: true,
    fourRowsVisible: true,
    identityHierarchy: true,
    itemHeightReadable: true,
    marketFactsFit: true,
  });
  await expect(identity.getByText("$UPEG", { exact: true })).toBeVisible();
  await expect(market.getByRole("group", { name: /^Price / })).toBeVisible();
  await expect(evidence).toContainText("8% duplicates");
  await expect(action).toBeVisible();
  await expectNoDocumentHorizontalOverflow(page);
  await expectNoNestedHorizontalOverflow(page, [".topbar", ".live-radar-item"]);
  await expect(page).toHaveScreenshot("radar-narrow-desktop.png", {
    animations: "disabled",
    caret: "hide",
    scale: "css",
  });

  await page.setViewportSize({ width: 1_210, height: 504 });
  const originalViewportLayout = await item.evaluate((element) => {
    const itemBox = element.getBoundingClientRect();
    const actionBox = element.querySelector("a")?.getBoundingClientRect();
    const facts = element.querySelectorAll<HTMLElement>(
      ".live-radar-market [role=group], .live-radar-item-evidence [role=group]",
    );
    return {
      actionContained:
        actionBox !== undefined &&
        actionBox.left >= itemBox.left &&
        actionBox.top >= itemBox.top &&
        actionBox.right <= itemBox.right &&
        actionBox.bottom <= itemBox.bottom &&
        itemBox.right - actionBox.right >= 16 &&
        actionBox.height >= 32,
      factsFit: [...facts].every(
        (fact) =>
          fact.scrollWidth <= fact.clientWidth + 1 && fact.scrollHeight <= fact.clientHeight + 1,
      ),
      fourRowsVisible:
        [...element.ownerDocument.querySelectorAll<HTMLElement>(".live-radar-item")].filter(
          (row) => {
            const box = row.getBoundingClientRect();
            return box.top >= 0 && box.bottom <= innerHeight;
          },
        ).length >= 4,
      itemHeightReadable: itemBox.height >= 84 && itemBox.height <= 120,
    };
  });
  expect(originalViewportLayout).toEqual({
    actionContained: true,
    factsFit: true,
    fourRowsVisible: true,
    itemHeightReadable: true,
  });
  await expectNoDocumentHorizontalOverflow(page);
  await expectNoNestedHorizontalOverflow(page, [".topbar", ".live-radar-item"]);
  await expect(page).toHaveScreenshot("radar-original-viewport.png", {
    animations: "disabled",
    caret: "hide",
    scale: "css",
  });

  for (const width of [768, 390]) {
    await page.setViewportSize({ width, height: 844 });
    const compactLayout = await item.evaluate((element) => {
      const itemBox = element.getBoundingClientRect();
      const actionBox = element.querySelector("a")?.getBoundingClientRect();
      const facts = element.querySelectorAll<HTMLElement>(
        ".live-radar-market [role=group], .live-radar-item-evidence [role=group]",
      );
      return {
        actionContained:
          actionBox !== undefined &&
          actionBox.left >= itemBox.left &&
          actionBox.top >= itemBox.top &&
          actionBox.right <= itemBox.right &&
          actionBox.bottom <= itemBox.bottom,
        contentsContained:
          element.scrollWidth <= element.clientWidth + 1 &&
          element.scrollHeight <= element.clientHeight + 1 &&
          [...element.children].every((child) => {
            const childBox = child.getBoundingClientRect();
            return childBox.top >= itemBox.top - 0.5 && childBox.bottom <= itemBox.bottom + 0.5;
          }),
        factsFit: [...facts].every(
          (fact) =>
            fact.scrollWidth <= fact.clientWidth + 1 &&
            fact.scrollHeight <= fact.clientHeight + 1 &&
            getComputedStyle(fact).textOverflow === "clip",
        ),
      };
    });
    expect(compactLayout).toEqual({
      actionContained: true,
      contentsContained: true,
      factsFit: true,
    });
    await expectNoDocumentHorizontalOverflow(page);
    await expectNoNestedHorizontalOverflow(page, [".topbar", ".live-radar-item"]);
  }
});
