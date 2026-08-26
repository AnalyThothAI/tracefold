import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, extname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";

import { createAppRouteObjects } from "@routes/router";
import { describe, expect, it } from "vitest";

const webRoot = join(dirname(fileURLToPath(import.meta.url)), "../..");
const srcRoot = join(webRoot, "src");
const testRoot = join(webRoot, "tests");
const sourceExtensions = new Set([".ts", ".tsx", ".css"]);

/*
 * #256 removed the #112 ReviewDesk from the browser: the four-view page, its route, its query hooks, its
 * fixtures, the four `/api/news/review*` endpoints, and — because those were the console's only writes —
 * the client's write verb itself. `tracefold news review queue | evidence | submit | accept-drafts |
 * external-miss` is the whole ReviewDesk contract now, appending the same `news_reviews` rows the learning
 * lane reads.
 *
 * What deliberately survives: `/api/news/events/{event_id}.review`, the accepted judgment for one Event.
 * That is a fact the CLI still produces and the Event detail still reports; what went with the page is the
 * link into it.
 */
const removedSourcePaths = ["features/news/ui/review"];

const removedTestPaths = [
  "component/features/news/NewsReviewPage.test.tsx",
  "e2e/golden-paths/price-review.spec.ts",
];

const retiredApiPaths = [
  "/api/news/review",
  "/api/news/review/tasks/",
  "/api/news/review/external-misses",
];

const retiredVocabulary = [
  "NewsReviewPage",
  "newsReviewPath",
  "newsReviewFixture",
  "useNewsReviewWithToken",
  "useNewsReviewEvidenceWithToken",
  "useSubmitNewsReview",
  "useSubmitExternalMiss",
  "NewsReviewQuery",
  "ReviewCheckIcon",
  "newsReview.css",
];

describe("ReviewDesk hard cut (#256)", () => {
  it("keeps the retired page and its tests deleted", () => {
    const survivors = [
      ...removedSourcePaths.map((path) => join(srcRoot, path)),
      ...removedTestPaths.map((path) => join(testRoot, path)),
    ].filter(existsSync);

    expect(survivors.map((path) => relative(webRoot, path))).toEqual([]);
  });

  it("keeps ReviewDesk routes, reads and vocabulary out of web/src", () => {
    const offenders = collectFiles(srcRoot)
      .filter((path) => sourceExtensions.has(extname(path)))
      // The generated OpenAPI mirror is regenerated from the served schema, not hand-edited.
      .filter((path) => relative(srcRoot, path) !== "lib/types/openapi.ts")
      .flatMap((path) => {
        const text = readFileSync(path, "utf8");
        return [...retiredApiPaths, ...retiredVocabulary]
          .filter((needle) => text.includes(needle))
          .map((needle) => `${relative(webRoot, path)}: ${needle}`);
      });

    expect(offenders).toEqual([]);
  });

  it("keeps the generated OpenAPI mirror free of ReviewDesk routes", () => {
    const openapi = readFileSync(join(srcRoot, "lib/types/openapi.ts"), "utf8");
    const paths = [...openapi.matchAll(/^ {4}"(\/[^"]+)": \{$/gm)].map((match) => match[1]);

    expect(paths.filter((path) => path.startsWith("/api/news/review"))).toEqual([]);
    // The per-Event judgment summary is a different fact and stays served.
    expect(openapi).toContain("NewsEventReviewSummaryData");
  });

  it("keeps ReviewDesk fixtures and mocks out of the frontend test surface", () => {
    const offenders = collectFiles(testRoot)
      .filter((path) => sourceExtensions.has(extname(path)))
      .filter((path) => !relative(testRoot, path).startsWith("architecture/"))
      .flatMap((path) => {
        const text = readFileSync(path, "utf8");
        return [...retiredApiPaths, ...retiredVocabulary]
          .filter((needle) => text.includes(needle))
          .map((needle) => `${relative(webRoot, path)}: ${needle}`);
      });

    expect(offenders).toEqual([]);
  });

  it("resolves /news/review through the not-found route with no redirect", () => {
    const paths = collectRoutePaths(createAppRouteObjects());

    expect(paths).not.toContain("news/review");
    expect(paths).toContain("*");
  });

  it("leaves the browser with no write verb at all", () => {
    // The ReviewDesk POSTs were the only two browser writes this project ever had. With them gone the
    // client facade offers `getApi` and nothing else, so "the console cannot write" is a property of the
    // transport rather than a rule someone has to remember.
    const client = readFileSync(join(srcRoot, "lib/api/client.ts"), "utf8");

    expect(client).toContain("export async function getApi");
    expect(client).not.toContain("postApi");
    expect(client).not.toMatch(/method:\s*"POST"/);
  });
});

function collectRoutePaths(routes: ReturnType<typeof createAppRouteObjects>): string[] {
  return routes.flatMap((route) => [
    ...(route.path === undefined ? [] : [route.path]),
    ...(route.children ? collectRoutePaths(route.children) : []),
  ]);
}

function collectFiles(root: string): string[] {
  return readdirSync(root).flatMap((entry) => {
    const path = join(root, entry);
    return statSync(path).isDirectory() ? collectFiles(path) : [path];
  });
}
