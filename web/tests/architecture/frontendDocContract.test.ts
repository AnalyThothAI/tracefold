import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { APP_NAVIGATION_GROUPS } from "@features/cockpit/ui/appNavigation";
import { describe, expect, it } from "vitest";

const webRoot = join(dirname(fileURLToPath(import.meta.url)), "../..");
const frontendDoc = readFileSync(join(webRoot, "../docs/FRONTEND.md"), "utf8");

describe("frontend documentation contract", () => {
  it("documents public feature barrels plus sanctioned shell entrypoints", () => {
    expect(frontendDoc).toContain("`@features/<name>`");
    expect(frontendDoc).toContain("`@features/<name>/shell`");
  });

  it("keeps documented navigation destinations aligned with the public model", () => {
    const navigation = APP_NAVIGATION_GROUPS.flatMap((group) => group.items);
    const targets = navigation.map((item) => item.to);

    expect(targets).toEqual(["/news", "/news/leverage", "/trading", "/news/oi"]);
    expect(navigation.flatMap((item) => item.children ?? [])).toEqual([]);
    expect(frontendDoc).toContain("`/news`");
    expect(frontendDoc).toContain("`/news/leverage`");
    expect(frontendDoc).toContain("`/trading`");
    expect(frontendDoc).toContain("`/news/oi`");
  });
});
