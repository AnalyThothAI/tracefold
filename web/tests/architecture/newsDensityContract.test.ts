import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const webRoot = join(dirname(fileURLToPath(import.meta.url)), "../..");

describe("News feed density contract", () => {
  it("clamps feed headlines to two lines and overlong detail titles to four", () => {
    const feedCss = readSource("src/features/news/newsFeed.css");
    const detailCss = readSource("src/features/news/newsDetail.css");
    const headlineRule = cssRule(feedCss, ".news-story-title h2");
    const detailTitleRule = cssRule(detailCss, ".news-story-hero h1.is-clamped");

    expect(headlineRule).toContain("display: -webkit-box");
    expect(headlineRule).toContain("overflow: hidden");
    expect(headlineRule).toContain("-webkit-box-orient: vertical");
    expect(headlineRule).toContain("-webkit-line-clamp: 2");
    expect(detailTitleRule).toContain("-webkit-line-clamp: 4");
  });

  it("keeps desktop feed chrome and ordinary cards on the compact density path", () => {
    const page = readSource("src/features/news/NewsPage.tsx");
    const chromeCss = readSource("src/features/news/news.css");
    const feedCss = readSource("src/features/news/newsFeed.css");

    expect(page).toContain('data-feed-density="compact"');
    expect(page).toMatch(
      /className="news-feed-toolbar"[\s\S]*?<NewsSectionTabs[\s\S]*?<NewsFeedControls/,
    );
    expect(cssRule(chromeCss, ".news-story-shell")).toContain("gap: 0.45rem");
    expect(cssRule(chromeCss, ".news-feed-toolbar")).toContain("justify-content: space-between");
    expect(cssRule(feedCss, ".news-story-primary")).toContain("padding: 0.52rem 0.7rem 0.42rem");
    expect(cssRule(feedCss, ".news-story-why")).toContain("padding: 0.26rem 0.7rem");
  });

  it("keeps the public Brief cards shrinkable and wraps its mobile header", () => {
    const briefCss = readSource("src/features/news/newsBrief.css");
    const responsiveCss = readSource("src/features/news/newsDetailResponsive.css");

    expect(cssRule(briefCss, ".news-brief-story")).toContain("min-width: 0");
    expect(cssRule(briefCss, ".news-brief-story")).toContain("overflow-wrap: anywhere");
    expect(cssRule(responsiveCss, ".news-brief-toolbar")).toContain("flex-direction: column");
  });

  it("stacks public Status and Sources without a mobile horizontal scroller", () => {
    const operationsCss = readSource("src/features/news/newsOperations.css");

    expect(cssRule(operationsCss, ".news-status-layer-grid")).toContain(
      "grid-template-columns: repeat(2, minmax(0, 1fr))",
    );
    expect(operationsCss).toMatch(
      /@media \(max-width: 767px\)[\s\S]*?\.news-status-layer-grid\s*{[^}]*grid-template-columns:\s*1fr;/,
    );
    expect(operationsCss).toMatch(
      /@media \(max-width: 767px\)[\s\S]*?\.news-source-card > dl[\s\S]*?grid-template-columns:\s*repeat\(2, minmax\(0, 1fr\)\);/,
    );
    expect(operationsCss).not.toContain("overflow-x: auto");
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
