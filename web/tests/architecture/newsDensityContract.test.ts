import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const webRoot = join(dirname(fileURLToPath(import.meta.url)), "../..");

/**
 * News console information model (issues #60, #82): every Event shows one server-owned outcome, every reason
 * is Chinese copy that arrives from the API, and internal identifiers live behind a technical-details fold.
 */
describe("News console contract", () => {
  it("tiers the row by outcome and clamps the headline", () => {
    const row = readSource("src/features/news/ui/feed/NewsEventRow.tsx");
    const rowCss = readSource("src/features/news/ui/feed/newsEventRow.css");
    const chip = readSource("src/features/news/ui/chrome/NewsDirectionChip.tsx");

    /*
     * Roughly three quarters of a day's Events stop short of a card. A capsule on each made a screenful of
     * equals, so the desktop row states its outcome as a *word*. The phone card drops the held badge entirely
     * (see the media query below), where a grey label on three cards out of four is pure noise. Either way it
     * is exactly one badge, and queue scheduling metadata has no reader-facing authority.
     */
    expect(row.match(/<NewsOutcomeBadge/g)).toHaveLength(1);
    expect(row).toContain("data-outcome-group={event.outcome.group}");
    expect(row).toContain('variant="text"');
    expect(row).not.toContain("event.priority");
    expect(row).not.toContain("data-priority");
    expect(rowCss).toMatch(
      /@media \(max-width: 767px\)[\s\S]*?\.news-event-row\[data-outcome-group="held"\] \.news-event-badge \{[^}]*display: none;/,
    );
    expect(row).toContain("event.outcome.reason_zh");
    // Direction is a toned chip of its own so a scan separates 利多 from 利空; it still renders the
    // server's word, never a frontend translation.
    expect(row.match(/<NewsDirectionChip /g)).toHaveLength(1);
    expect(chip).toContain("triage.direction_zh");
    expect(chip).toContain("triage.magnitude_zh");
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
    // The row opens the Event through the headline link, never through a click handler on the article, so
    // the keyboard reaches it and the row keeps one accessible name. The two controls inside it (select,
    // expand) are real buttons with their own labels and their own handlers.
    expect(cssRule(rowCss, ".news-event-headline a::after")).toContain("position: absolute");
    expect(row).not.toMatch(/<article\b[^>]*onClick=/);
  });

  it("gets its business vocabulary from the server, not from a frontend enum table", () => {
    const labels = readSource("src/features/news/model/newsLabels.ts");
    const detail = readSource("src/features/news/ui/detail/NewsEventDetailPage.tsx");
    const timeline = readSource("src/features/news/ui/detail/NewsTimeline.tsx");
    const status = readSource("src/features/news/ui/status/NewsStatusPage.tsx");

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
    expect(timeline).toContain("step.summary_zh");
    expect(detail).toContain("outcome");
    expect(status).toContain("reason.label_zh");
    expect(status).toContain("summary_zh");
    expect(status).not.toContain("JSON.stringify(status.control");
  });

  it("keeps the case detail as conclusion → timeline → members/labels → folded technical details", () => {
    const detail = readSource("src/features/news/ui/detail/NewsEventDetailPage.tsx");
    const detailCss = readSource("src/features/news/ui/detail/newsDetail.css");
    const feed = readSource("src/features/news/ui/feed/NewsFeedPage.tsx");
    const shellRule = cssRule(detailCss, ".news-panel.news-detail-shell");

    expect(detail.match(/className="news-detail-shell"/g)).toHaveLength(1);
    expect(detail).toContain('archetype="case"');
    expect(detail).toContain("RouteBackLink");
    // Reading order in the rendered document, not import order at the top of the file.
    expect(detail.indexOf("news-detail-hero")).toBeLessThan(detail.indexOf("<NewsTimeline "));
    expect(detail.indexOf("<NewsTimeline ")).toBeLessThan(detail.indexOf("<TechnicalDetails "));
    expect(detail).toMatch(/<NewsTechnical summary="技术详情/);
    expect(feed).not.toContain("news-detail-shell");
    expect(shellRule).toContain("grid-template-rows: none");
    expect(shellRule).toContain("grid-auto-rows: max-content");

    // Review writes belong to ReviewDesk, not the Event detail read model.
    expect(detail).not.toContain("labelCommand");
    expect(detail).not.toMatch(/useMutation|postApi/);
    for (const retired of [
      "src/features/news/NewsVerdictPanel.tsx",
      "src/features/news/newsVerdict.css",
      "src/features/news/newsBrief.css",
      "src/features/news/newsAudit.css",
      "src/features/news/newsOperations.css",
      "src/features/news/NewsOperationsPage.tsx",
      "src/features/news/NewsSectionTabs.tsx",
    ]) {
      expect(existsSync(join(webRoot, retired)), `${retired} must stay deleted`).toBe(false);
    }
  });

  it("gives the feed task tabs with server counts, a time window, and a compact scan surface", () => {
    const page = readSource("src/features/news/ui/feed/NewsFeedPage.tsx");
    const toolbar = readSource("src/features/news/ui/feed/NewsFeedToolbar.tsx");
    const toolbarCss = readSource("src/features/news/ui/feed/newsFeedToolbar.css");

    expect(page).toContain('archetype="scan"');
    expect(page).toMatch(/<NewsFunnelCard[\s\S]*?<NewsFeedToolbar[\s\S]*?news-event-list-header/);
    expect(toolbar).toMatch(/role="tablist"/);
    expect(toolbar).toContain(
      'const OUTCOME_TABS: Array<NewsFeedOutcome | null> = ["pushed", "held", "pending", null]',
    );
    expect(toolbar).toContain("NEWS_FEED_HOURS");
    // The counts are the server's split of the current filter — never derived in the browser.
    expect(toolbar).toContain("counts[value]");
    expect(page).toContain("firstPage?.counts");
    expect(cssRule(toolbarCss, ".news-feed-toolbar")).toContain("justify-content: space-between");
    expect(cssRule(toolbarCss, ".news-feed-toolbar")).toContain("position: sticky");
  });

  it("keeps feature CSS from reaching the primitives rendered inside a feature card", () => {
    /*
     * `app.features` beats `app.primitives` whatever the specificity, so a bare element selector under a
     * feature class silently restyles every primitive underneath it. `.news-funnel-card b` did exactly that:
     * the five 24 h figures rendered at the header sentence's 11.5px instead of the 21px `--type-metric` the
     * design asks for, and lost their accent and caution tones on the way. The rule belongs to the sentence.
     */
    const funnelCss = readSource("src/features/news/ui/feed/newsFunnel.css").replace(
      /\/\*[\s\S]*?\*\//g,
      "",
    );

    expect(funnelCss).toContain(".news-funnel-summary b");
    expect(funnelCss).not.toMatch(/\.news-funnel-card\s+b\b/);
  });

  it("renders every field the instrument-universe summary carries", () => {
    /*
     * `by_class` and `dangling_aliases` were served for a whole release without appearing anywhere — and
     * `dangling_aliases` is an alarm the server states a target for. A card that silently drops fields is
     * the same failure as a stage list that silently drops groups, so it gets the same kind of gate.
     */
    const page = readSource("src/features/news/ui/status/NewsInstrumentUniverse.tsx");
    const generated = readSource("src/lib/types/openapi.ts");

    const fields = [
      ...(generated.match(/NewsInstrumentUniverse:\s*\{([\s\S]*?)\n {8}\}/)?.[1] ?? "").matchAll(
        /^\s{12}(\w+)\??:/gm,
      ),
    ].map((match) => match[1]);
    expect(fields).toContain("dangling_aliases");
    expect(fields.length).toBeGreaterThan(4);

    for (const field of fields) {
      expect(page, `标的表快照 must render instruments.${field}`).toContain(`universe.${field}`);
    }
  });

  it("renders every reason stage the API contract can send", () => {
    /*
     * `REASON_STAGE_ORDER` is the render filter, not just a sort: a stage missing from it is dropped
     * silently. `ungrounded` shipped computed, labelled and documented but invisible for exactly that
     * reason (#87 review), so the list is checked against the generated contract rather than by eye.
     */
    const page = readSource("src/features/news/ui/status/NewsStatusPage.tsx");
    const generated = readSource("src/lib/types/openapi.ts");
    const labels = readSource("src/features/news/model/newsLabels.ts");

    const contractStages = (
      generated.match(/NewsReasonCountData:[\s\S]*?stage:\s*([^;]+);/)?.[1] ?? ""
    )
      .split("|")
      .map((part) => part.trim().replace(/^"|"$/g, ""))
      .filter(Boolean);
    expect(contractStages.length).toBeGreaterThan(0);

    const rendered = (page.match(/const REASON_STAGE_ORDER = \[([^\]]+)\]/)?.[1] ?? "")
      .split(",")
      .map((part) => part.trim().replace(/^"|"$/g, ""))
      .filter(Boolean);

    expect(rendered.slice().sort()).toEqual(contractStages.slice().sort());
    // Every stage also needs a title and a tone, or it renders as a bare enum key.
    for (const stage of contractStages) {
      expect(labels, `${stage} needs a Chinese group title`).toContain(`${stage}:`);
    }
  });

  it("stacks the status page's health cards and grids without a mobile horizontal scroller", () => {
    const statusCss = readSource("src/features/news/ui/status/newsStatus.css");
    const statusPage = readSource("src/features/news/ui/status/NewsStatusPage.tsx");

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
    // Pause and mute are written by `tracefold news control`; the browser is not a second writer.
    expect(statusPage).not.toMatch(/暂停推送|解除|useMutation|postApi/);
    // #126: which Strategies feed News is provider account configuration. The page reads no Strategy field
    // and renders no Strategy section — Tracefold neither chooses nor filters them, so a figure here would
    // only restate the provider's dashboard. (Prose explaining the absence is fine; a heading is not.)
    expect(statusPage).not.toMatch(/strategy_ids|strategy_warnings|provider_strategy_count/);
    expect(statusPage).not.toContain(">Strategy<");
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
