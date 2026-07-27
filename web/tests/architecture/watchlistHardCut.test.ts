import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import { extname, join, relative } from "node:path";

import { describe, expect, it } from "vitest";

const srcRoot = join(process.cwd(), "src");
const sourceExtensions = new Set([".css", ".ts", ".tsx"]);

describe("watchlist and notifications hard cut", () => {
  it("deletes their routes, features, shared controls, and socket state", () => {
    for (const removed of [
      "routes/watchlist.route.tsx",
      "features/watchlist",
      "features/notifications",
      "shared/ui/HandleFilter.tsx",
      "shared/ui/HandleFilter.css",
    ]) {
      expect(existsSync(join(srcRoot, removed))).toBe(false);
    }
  });

  it("keeps retired product vocabulary out of production source", () => {
    const blocked = [
      "/watchlist",
      "Watchlist",
      "notificationItems",
      "NotificationBell",
      "is_watched",
      "matched_handles",
      "watched_mentions",
      "token flow scope",
    ];
    const offenders = collectFiles(srcRoot)
      .filter((path) => sourceExtensions.has(extname(path)))
      .flatMap((path) => {
        const text = readFileSync(path, "utf8");
        return blocked
          .filter((pattern) => text.includes(pattern))
          .map((pattern) => `${relative(srcRoot, path)}: ${pattern}`);
      });

    expect(offenders).toEqual([]);
  });
});

function collectFiles(root: string): string[] {
  return readdirSync(root).flatMap((entry) => {
    const path = join(root, entry);
    return statSync(path).isDirectory() ? collectFiles(path) : [path];
  });
}
