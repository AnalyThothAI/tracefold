import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import {
  APP_NAVIGATION_GROUPS,
  type AppNavigationItem,
} from "../../src/features/cockpit/ui/appNavigation";

const webRoot = join(dirname(fileURLToPath(import.meta.url)), "../..");
const repoRoot = join(webRoot, "..");
const frontendDoc = readFileSync(join(repoRoot, "docs/FRONTEND.md"), "utf8");
const frontendVerificationSkillPath = join(
  repoRoot,
  ".agents/skills/tracefold-frontend-verification/SKILL.md",
);
const frontendVerificationSkill = existsSync(frontendVerificationSkillPath)
  ? readFileSync(frontendVerificationSkillPath, "utf8")
  : null;

describe("frontend documentation contract", () => {
  it("keeps CSS ownership docs aligned with the architecture harness", () => {
    const cssHarness = readFileSync(
      join(webRoot, "tests/architecture/cssArchitectureHarness.test.ts"),
      "utf8",
    );
    const cssResponsiveHarness = readFileSync(
      join(webRoot, "tests/architecture/cssResponsiveContract.test.ts"),
      "utf8",
    );

    for (const bucket of stringSetValues(cssHarness, "retiredGlobalCssBuckets")) {
      expect(frontendDoc).toContain(`\`${bucket}\``);
      if (frontendVerificationSkill !== null) {
        expect(frontendVerificationSkill).toContain(`\`${bucket}\``);
      }
    }

    const budget = cssResponsiveHarness.match(/above the (?<budget>\d+)-line budget/)?.groups
      ?.budget;
    expect(budget).toBe("500");
    expect(frontendDoc).toContain("above 500 lines");
    expect(frontendDoc).not.toContain("above 700 lines");
  });

  it("keeps the frontend verification skill aligned with architecture commands", () => {
    if (frontendVerificationSkill === null) {
      return;
    }
    expect(frontendVerificationSkill).toContain("`cd web && npm run lint`");
    expect(frontendVerificationSkill).toContain("`cd web && npm run test:architecture`");
    expect(frontendVerificationSkill).toContain("`cd web && npm run typecheck`");
  });

  it("keeps the frontend verification skill aligned with data ownership checks", () => {
    const dataOwnershipHarness = readFileSync(
      join(webRoot, "tests/architecture/frontendDataOwnership.test.ts"),
      "utf8",
    );
    const forbiddenPrimitives = [
      { harnessNeedle: "useQuery", skillToken: "useQuery" },
      { harnessNeedle: "useMutation", skillToken: "useMutation" },
      { harnessNeedle: "useInfiniteQuery", skillToken: "useInfiniteQuery" },
      { harnessNeedle: "getApi", skillToken: "getApi" },
      { harnessNeedle: "postApi", skillToken: "postApi" },
      { harnessNeedle: "queryClient\\.set", skillToken: "queryClient.set" },
    ];

    expect(frontendDoc).toContain("`frontendDataOwnership.test.ts`");
    if (frontendVerificationSkill === null) {
      return;
    }
    expect(frontendVerificationSkill).toContain("`frontendDataOwnership.test.ts`");
    for (const { harnessNeedle, skillToken } of forbiddenPrimitives) {
      expect(dataOwnershipHarness).toContain(harnessNeedle);
      expect(frontendVerificationSkill).toContain(`\`${skillToken}\``);
    }
  });

  it("documents public feature barrels plus sanctioned shell entrypoints", () => {
    expect(frontendDoc).toContain("`@features/<name>`");
    expect(frontendDoc).toContain("`@features/<name>/shell`");
  });

  it("keeps primary route docs aligned with the app navigation tree", () => {
    const documentedRoutes = [{ term: "News", to: "/news" }];
    const navigationItems = flattenNavigation(
      APP_NAVIGATION_GROUPS.flatMap((group) => group.items),
    );
    const navigationTargets = navigationItems.map((item) => item.to);

    for (const { term, to } of documentedRoutes) {
      expect(frontendDoc).toContain(term);
      expect(navigationTargets).toContain(to);
    }
    expect(navigationTargets).toEqual(["/news"]);
    expect(navigationTargets).not.toContain("/");
    expect(frontendDoc).not.toMatch(/Token Radar|RadarPage|live-radar|features\/live/);

    const topbar = readFileSync(join(webRoot, "src/features/cockpit/ui/CockpitTopbar.tsx"), "utf8");
    expect(frontendDoc).toContain("no browser Ops route");
    expect(navigationTargets).not.toContain("/ops");
    expect(topbar).not.toContain("opsPath");
    expect(topbar).toContain("healthAnomaly");
    expect(topbar).toContain("topbar-anomaly");
  });

  it("describes the News console without the retired GMGN or Macro lanes", () => {
    expect(frontendDoc).toContain("gmgnLaneHardCut.test.ts");
    expect(frontendDoc).toContain("`/news?q=<query>`");
    expect(frontendDoc).toContain("`news search`");
    expect(frontendDoc).toContain("搜索新闻事件 / 标题 / 资产");
    expect(frontendDoc).toContain("no market-mark table");
    for (const retired of [
      "Token Case route",
      "Search route",
      "Socket lifecycle",
      "SearchShell",
      "shared/socket",
      "IntelSocket",
      "/api/search",
      "/api/token-case",
      "/api/target-posts",
      "/api/live-market",
      "market_targets",
      "`marks[]`",
      "/search?q=",
      "GMGN action link",
    ]) {
      expect(frontendDoc, `docs/FRONTEND.md still describes ${retired}`).not.toContain(retired);
    }
  });
});

function flattenNavigation(items: AppNavigationItem[]): AppNavigationItem[] {
  return items.flatMap((item) => [item, ...flattenNavigation(item.children ?? [])]);
}

function stringSetValues(source: string, variableName: string): string[] {
  const match = source.match(
    new RegExp(`const ${variableName} = new Set\\(\\[([\\s\\S]*?)\\]\\);`),
  );
  expect(match, `${variableName} must be declared as a string Set`).not.toBeNull();
  return [...(match?.[1] ?? "").matchAll(/"([^"]+)"/g)].map((item) => item[1]);
}
