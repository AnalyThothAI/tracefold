import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const webRoot = join(dirname(fileURLToPath(import.meta.url)), "../..");
const srcRoot = join(webRoot, "src");

/**
 * Geometry the v8 console artifact fixes, asserted as the strings it fixes them to (#280).
 *
 * Every other frontend gate here asks whether a rule is *allowed* — the right namespace, the right layer,
 * a token instead of a hex. None of them ask whether a rule is the *value the artifact drew*, and that is
 * the gap this file exists to close. The four pages rebuilt for #207/#256 all matched the artifact's
 * structure and none of them matched its measurements: `336px / 856px` had become `1fr / 1.22fr`, a trade
 * column the artifact lets grow had become a fixed 150px, and 14px between navigation groups had become
 * zero. Each was a plausible local choice, and together they were a console that did not look like the
 * design it was built from.
 *
 * The visual baselines cannot catch this class: `maxDiffPixelRatio` passes a whole column moving by fifty
 * pixels, and a screenshot cannot say which of two plausible layouts was intended. A track list can.
 *
 * Changing a value here is allowed — the artifact is not frozen. Changing it *without noticing* is what
 * this stops. If the design moves, move the expectation and say which artifact version moved it.
 */
const geometry: Array<{
  file: string;
  property: string;
  selector: string;
  value: string;
  why: string;
}> = [
  {
    file: "styles/tokens.css",
    selector: ":root",
    property: "--shell-sidebar-width",
    value: "204px",
    why: "the artifact's aside is a fixed 204px in every frame that has one",
  },
  {
    file: "styles/tokens.css",
    selector: ":root",
    property: "--shell-topbar-height",
    value: "54px",
    why: "the topbar and the sidebar's brand block share one height, and the sticky offsets are cut from it",
  },
  {
    file: "features/cockpit/ui/AppSidebar.css",
    selector:
      ".cockpit-app-sidebar-group + .cockpit-app-sidebar-group .cockpit-app-sidebar-group-heading",
    property: "margin-top",
    value: "14px",
    why: "WORKBENCH and SYSTEM · 数据健康 are two runs, not one list with a label in the middle",
  },
  {
    file: "features/news/ui/leverage/newsLeverage.css",
    selector: ".news-panel.news-leverage-shell",
    property: "max-width",
    value: "1360px",
    why: "the artifact gives this page and the OI audit twenty more than the reading surfaces",
  },
  {
    file: "features/news/ui/leverage/newsLeverage.css",
    selector: ".news-leverage-body",
    property: "grid-template-columns",
    value: "minmax(288px, 336px) minmax(0, 1fr)",
    why: "a list of five-line summaries beside a document, not two columns of comparable weight",
  },
  {
    file: "features/news/ui/leverage/newsLeverageDetail.css",
    selector: ".news-leverage-thesis",
    property: "grid-template-columns",
    value: "minmax(0, 1.3fr) minmax(0, 1fr)",
    why: "the setup sentence carries the case; the key/value list qualifies it",
  },
  {
    file: "features/news/ui/leverage/newsLeverageDetail.css",
    selector: ".news-leverage-plane-items",
    property: "grid-template-columns",
    value: "repeat(auto-fit, minmax(148px, 1fr))",
    why: "five plane cells across at pane width, wrapping rather than shrinking under it",
  },
  {
    file: "features/news/ui/leverage/newsLeverageDetail.css",
    selector: ".news-leverage-evidence > div",
    property: "grid-template-columns",
    value: "78px 62px minmax(0, 1fr)",
    why: "the four evidence states line up in one column; the note takes what is left",
  },
  {
    file: "features/news/ui/leverage/newsLeverageDetail.css",
    selector: ".news-leverage-columns",
    property: "grid-template-columns",
    value: "minmax(0, 1.2fr) minmax(0, 1fr)",
    why: "the timeline is wider than the capital loop beside it",
  },
  {
    file: "features/news/ui/oi/newsOi.css",
    selector: ".news-panel.news-oi-shell",
    property: "max-width",
    value: "1360px",
    why: "the frame table's trade column is the first thing a narrower measure clips",
  },
  {
    file: "features/news/ui/oi/newsOiFrameTable.css",
    selector: ".news-oi-head, .news-oi-row-main",
    property: "grid-template-columns",
    value: "46px 96px 64px 62px 64px 64px 62px 46px 118px 140px 100px minmax(170px, 1fr)",
    why: "twelve tracks, one cell for the 1H/4H pair, and a trade column that takes the remainder",
  },
  {
    file: "features/trading/ui/trading.css",
    selector: ".trading-exposure-head, .trading-exposure-row",
    property: "grid-template-columns",
    value: "96px 74px 150px 168px 128px 118px minmax(180px, 1fr) 96px",
    why: "the order state and the sentence beside it are the page's whole claim and must not ellipsise",
  },
  {
    file: "features/trading/ui/trading.css",
    selector: ".trading-closed-head, .trading-closed-row",
    property: "grid-template-columns",
    value: "96px 74px 140px minmax(150px, 1fr) 118px 84px",
    why: "the same eight-column reading at six columns, so the two tables scan as one family",
  },
  {
    file: "features/trading/ui/trading.css",
    selector: ".trading-columns",
    property: "grid-template-columns",
    value: "minmax(0, 1.32fr) minmax(0, 1fr)",
    why: "今日已了结 is a table and 今日案例去向 is five bars; the artifact weights them accordingly",
  },
];

describe("v8 artifact geometry contract", () => {
  it.each(geometry)("$file pins $property on $selector", ({ file, property, selector, value }) => {
    expect(declaration(file, selector, property)).toBe(value);
  });

  it("keeps the OI frame table's floor at its fixed tracks and no wider", () => {
    /*
     * The artifact bakes its trade column's *rendered* width into `min-width`, which forces a horizontal
     * scrollbar at the width the artifact itself is drawn at. The floor here is the fixed tracks plus
     * their gutters plus the row padding, so the flexible column is what absorbs a wider frame — the
     * artifact's intent, without the artifact's own arithmetic error.
     */
    const css = read("features/news/ui/oi/newsOiFrameTable.css");
    const tracks = declaration(
      "features/news/ui/oi/newsOiFrameTable.css",
      ".news-oi-head, .news-oi-row-main",
      "grid-template-columns",
    );
    const fixed = [...tracks.replace(/minmax\([^)]*\)/g, "").matchAll(/(\d+)px/g)].map((match) =>
      Number(match[1]),
    );
    // The flexible track absorbs the remainder above its own minimum, and that minimum is part of the floor.
    const flexible = Number(/minmax\((\d+)px/.exec(tracks)?.[1] ?? 0);
    const gutters = fixed.length * 10;
    const padding = 28;
    const floor = fixed.reduce((total, width) => total + width, 0) + flexible + gutters + padding;

    expect(fixed).toHaveLength(11);
    expect(flexible).toBe(170);
    for (const declared of css.matchAll(/min-width:\s*(\d+)px;/g)) {
      expect(Number(declared[1])).toBe(floor);
    }
  });
});

/** The stylesheet with comments removed: a `/* … *\/` between two declarations is not a rule boundary. */
function read(file: string): string {
  return readFileSync(join(srcRoot, file), "utf8").replace(/\/\*[\s\S]*?\*\//g, "");
}

/**
 * One declaration out of one rule, whitespace-normalised.
 *
 * Deliberately dumb: it matches the selector text as written rather than parsing the cascade, so a rule
 * that moved to a different selector fails here loudly instead of silently matching something else.
 */
function declaration(file: string, selector: string, property: string): string {
  const css = read(file);
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&").replace(/,\s*/g, ",\\s*");
  const rule = new RegExp(`(?:^|[};/])\\s*${escaped}\\s*\\{([^}]*)\\}`, "m").exec(css);
  if (!rule) throw new Error(`${file} has no rule for ${selector}`);
  const declared = new RegExp(`(?:^|;)\\s*${property}\\s*:([^;]*);`).exec(rule[1]);
  if (!declared) throw new Error(`${file} rule ${selector} declares no ${property}`);
  return declared[1].trim().replace(/\s+/g, " ");
}
