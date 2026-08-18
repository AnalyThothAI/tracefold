import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const webRoot = join(dirname(fileURLToPath(import.meta.url)), "../..");

describe("News feed density contract", () => {
  it("clamps Event feed headlines and context lines to two lines", () => {
    const rowCss = readSource("src/features/news/newsEventRow.css");
    const headlineRule = cssRule(rowCss, ".news-event-title h2");
    const contextRule = cssRule(rowCss, ".news-event-leader-title,\n  .news-event-context-line");

    expect(headlineRule).toContain("display: -webkit-box");
    expect(headlineRule).toContain("overflow: hidden");
    expect(headlineRule).toContain("-webkit-box-orient: vertical");
    expect(headlineRule).toContain("-webkit-line-clamp: 2");
    expect(contextRule).toContain("-webkit-line-clamp: 2");
  });

  it("keeps News case-detail chrome and content in adjacent content-sized rows", () => {
    const detailPage = readSource("src/features/news/NewsEventDetailPage.tsx");
    const feedPage = readSource("src/features/news/NewsPage.tsx");
    const detailCss = readSource("src/features/news/newsDetail.css");
    const shellRule = cssRule(detailCss, ".news-panel.news-detail-shell");

    expect(detailPage.match(/className="news-panel news-detail-shell"/g)).toHaveLength(1);
    expect(detailPage).toContain('data-page-archetype="case"');
    expect(detailPage).toContain("RouteBackLink");
    expect(feedPage).not.toContain("news-detail-shell");
    expect(feedPage).not.toContain("radar-panel");
    expect(shellRule).toContain("grid-template-rows: none");
    expect(shellRule).toContain("grid-auto-rows: max-content");
  });

  it("keeps compact Event rows readable with server-owned triage and delivery facts", () => {
    const page = readSource("src/features/news/NewsPage.tsx");
    const row = readSource("src/features/news/NewsEventRow.tsx");
    const chromeCss = readSource("src/features/news/news.css");

    expect(page).toContain('data-feed-density="compact"');
    expect(page).toMatch(
      /className="news-feed-toolbar"[\s\S]*?<NewsSectionTabs[\s\S]*?<NewsFeedControls/,
    );
    expect(cssRule(chromeCss, ".news-feed-shell")).toContain("gap: 0.45rem");
    expect(cssRule(chromeCss, ".news-feed-toolbar")).toContain("justify-content: space-between");
    expect(row).toMatch(
      /className="news-event-classification"[\s\S]*?news-event-priority[\s\S]*?news-event-decision[\s\S]*?news-event-admission[\s\S]*?<OpenNewsScoreBadge/,
    );
    expect(row).toMatch(/<OpenNewsScoreBadge score={event\.provider_score_max}/);
    expect(row).toMatch(/<NewsTriageStrip triage={triage}/);
    expect(row).toMatch(/<NewsDeliveryState delivery={event\.delivery/);
    expect(row).not.toMatch(/importance_score|identity_evidence|provider_evidence/);
  });

  it("splits the Event row owner CSS from the feed shell and keeps both under budget", () => {
    const feedCss = readSource("src/features/news/newsFeed.css");
    const rowCss = readSource("src/features/news/newsEventRow.css");

    expect(feedCss).not.toContain(".news-event-row");
    expect(rowCss).toContain(".news-event-row");
    expect(feedCss.split(/\r?\n/).length).toBeLessThanOrEqual(500);
    expect(rowCss.split(/\r?\n/).length).toBeLessThanOrEqual(500);
    for (const retired of [
      "src/features/news/newsBrief.css",
      "src/features/news/newsAudit.css",
      "src/features/news/newsOperations.css",
      "src/features/news/newsDetailResponsive.css",
      "src/features/news/NewsOperationsPage.tsx",
    ]) {
      expect(existsSync(join(webRoot, retired)), `${retired} must stay deleted`).toBe(false);
    }
  });

  it("stacks the four public Status layers without a mobile horizontal scroller", () => {
    const statusCss = readSource("src/features/news/newsStatus.css");
    const statusPage = readSource("src/features/news/NewsStatusPage.tsx");

    expect(cssRule(statusCss, ".news-status-layer-grid,\n  .news-status-control-grid")).toContain(
      "grid-template-columns: repeat(2, minmax(0, 1fr))",
    );
    expect(statusCss).toMatch(
      /@media \(max-width: 767px\)[\s\S]*?\.news-status-layer-grid,\s*\.news-status-control-grid\s*{[^}]*grid-template-columns:\s*1fr;/,
    );
    expect(statusCss).not.toContain("overflow-x: auto");
    expect(statusPage.match(/<StatusLayer/g)).toHaveLength(4);
    expect(statusPage).not.toMatch(/news-source-|newsSources|Brief/);
  });
});

function readSource(relativePath: string): string {
  return readFileSync(join(webRoot, relativePath), "utf8");
}

function cssRule(source: string, selector: string): string {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = source.match(new RegExp(`${escaped}\\s*\\{([^}]*)\\}`));
  expect(match, `Missing CSS rule ${selector}`).not.toBeNull();
  return match?.[1] ?? "";
}
