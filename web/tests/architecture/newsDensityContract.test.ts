import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const webRoot = join(dirname(fileURLToPath(import.meta.url)), "../..");

/**
 * News console information model (issue #60): every Event shows one server-owned outcome, every reason is
 * Chinese copy that arrives from the API, and internal identifiers live behind a technical-details fold.
 */
describe("News console contract", () => {
  it("renders exactly one outcome badge per Event row and clamps the headline", () => {
    const row = readSource("src/features/news/NewsEventRow.tsx");
    const rowCss = readSource("src/features/news/newsEventRow.css");

    expect(row.match(/<NewsOutcomeBadge /g)).toHaveLength(1);
    expect(row).toContain("event.outcome");
    expect(row).toContain("triage.direction_zh");
    expect(row).toContain("triage.event_type_zh");
    // Bare rule / admission / decision keys must never be rendered as row copy.
    for (const banned of [
      "admissionLabel(",
      "decisionLabel(",
      "override_rule",
      "throttled_by",
      "final_decision",
    ]) {
      expect(row, `NewsEventRow.tsx must not render ${banned}`).not.toContain(banned);
    }
    const headlineRule = cssRule(rowCss, ".news-event-headline a");
    expect(headlineRule).toContain("display: -webkit-box");
    expect(headlineRule).toContain("-webkit-line-clamp: 2");
    expect(cssRule(rowCss, ".news-event-original")).toContain("-webkit-line-clamp: 1");
  });

  it("gets its business vocabulary from the server, not from a frontend enum table", () => {
    const labels = readSource("src/features/news/newsLabels.ts");
    const detail = readSource("src/features/news/NewsEventDetailPage.tsx");
    const status = readSource("src/features/news/NewsStatusPage.tsx");

    // The only enum→copy maps left in the frontend are UI affordances (tabs, health levels, stage titles).
    for (const retired of [
      "ADMISSION_LABELS",
      "DECISION_LABELS",
      "DIRECTION_LABELS",
      "STAGE_LABELS",
      "SCOPE_LABELS",
    ]) {
      expect(labels).not.toContain(retired);
    }
    expect(labels).toContain("outcomeTone");
    expect(labels).toContain("healthTone");
    expect(detail).toContain("step.summary_zh");
    expect(detail).toContain("outcome");
    expect(status).toContain("reason.label_zh");
    expect(status).toContain("summary_zh");
    expect(status).not.toContain("JSON.stringify(status.control");
  });

  it("keeps the case detail as conclusion → timeline → members/labels → folded technical details", () => {
    const detail = readSource("src/features/news/NewsEventDetailPage.tsx");
    const detailCss = readSource("src/features/news/newsDetail.css");
    const feed = readSource("src/features/news/NewsPage.tsx");
    const shellRule = cssRule(detailCss, ".news-panel.news-detail-shell");

    expect(detail.match(/className="news-panel news-detail-shell"/g)).toHaveLength(1);
    expect(detail).toContain('data-page-archetype="case"');
    expect(detail).toContain("RouteBackLink");
    expect(detail.indexOf("news-detail-hero")).toBeLessThan(detail.indexOf("news-timeline"));
    expect(detail.indexOf("news-timeline")).toBeLessThan(detail.indexOf("news-technical"));
    expect(detail).toMatch(/<details className="news-technical">/);
    expect(feed).not.toContain("news-detail-shell");
    expect(shellRule).toContain("grid-template-rows: none");
    expect(shellRule).toContain("grid-auto-rows: max-content");
    for (const retired of [
      "src/features/news/NewsVerdictPanel.tsx",
      "src/features/news/newsVerdict.css",
      "src/features/news/newsBrief.css",
      "src/features/news/newsAudit.css",
      "src/features/news/newsOperations.css",
      "src/features/news/NewsOperationsPage.tsx",
    ]) {
      expect(existsSync(join(webRoot, retired)), `${retired} must stay deleted`).toBe(false);
    }
  });

  it("gives the feed task tabs, a time window, and a compact scan surface", () => {
    const page = readSource("src/features/news/NewsPage.tsx");
    const chromeCss = readSource("src/features/news/news.css");

    expect(page).toContain('data-feed-density="compact"');
    expect(page).toContain('data-page-archetype="scan"');
    expect(page).toMatch(/role="tablist"/);
    expect(page).toContain("NEWS_FEED_OUTCOMES");
    expect(page).toContain("NEWS_FEED_HOURS");
    expect(page).toMatch(
      /className="news-feed-toolbar"[\s\S]*?<NewsSectionTabs[\s\S]*?<OutcomeTabs[\s\S]*?<NewsFeedControls/,
    );
    expect(cssRule(chromeCss, ".news-feed-toolbar")).toContain("justify-content: space-between");
    expect(readSource("src/features/news/newsFeed.css").split(/\r?\n/).length).toBeLessThanOrEqual(
      300,
    );
    expect(
      readSource("src/features/news/newsEventRow.css").split(/\r?\n/).length,
    ).toBeLessThanOrEqual(400);
  });

  it("stacks the status page's health cards and grids without a mobile horizontal scroller", () => {
    const statusCss = readSource("src/features/news/newsStatus.css");
    const statusPage = readSource("src/features/news/NewsStatusPage.tsx");

    expect(cssRule(statusCss, ".news-health-grid")).toContain(
      "grid-template-columns: repeat(4, minmax(0, 1fr))",
    );
    expect(statusCss).toMatch(
      /@media \(max-width: 767px\)[\s\S]*?\.news-health-grid,\s*\.news-status-grid\s*{[^}]*grid-template-columns:\s*1fr;/,
    );
    expect(statusCss).not.toContain("overflow-x: auto");
    expect(statusPage).toContain("HEALTH_ITEM_KEYS.map");
    expect(statusPage).toContain("funnel_24h");
    expect(statusPage).toContain("reasons_24h");
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
