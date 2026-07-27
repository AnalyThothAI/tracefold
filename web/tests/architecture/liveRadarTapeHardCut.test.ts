import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, extname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const webRoot = join(dirname(fileURLToPath(import.meta.url)), "../..");
const srcRoot = join(webRoot, "src");
const liveRoot = join(srcRoot, "features/live");
const sourceExtensions = new Set([".css", ".ts", ".tsx"]);

describe("Live Radar Tape frontend hard cut", () => {
  it("removes retired Tape, recent-read, mobile-task, and event-buffer ownership", () => {
    for (const removed of [
      "features/live/api/useLiveRecentQuery.ts",
      "features/live/liveTapeModel.ts",
      "features/live/model/liveMobileTask.ts",
      "features/live/ui/LiveSignalTape.tsx",
      "features/live/ui/LiveTaskNav.tsx",
    ]) {
      expect(existsSync(join(srcRoot, removed))).toBe(false);
    }

    const ownedFiles = [
      ...collectSourceFiles(liveRoot),
      join(srcRoot, "routes/live.route.tsx"),
      join(srcRoot, "routes/shellChromeData.ts"),
      join(srcRoot, "shared/socket/IntelSocketProvider.tsx"),
      join(srcRoot, "shared/socket/socketContext.ts"),
      join(srcRoot, "shared/socket/socketTypes.ts"),
    ];
    const forbidden = [
      /\bLiveSignalTape\b/,
      /\bLiveTaskNav\b/,
      /\bLiveMobileTask\b/,
      /\buseLiveRecentQuery\b/,
      /\beventItems\b/,
      /["']\/api\/recent["']/,
      /\.live-signal-tape\b/,
      /\.live-task-nav\b/,
      /\.tape-/,
      /\bmobile-task-/,
    ];
    const offenders = ownedFiles.flatMap((path) => {
      const source = readFileSync(path, "utf8");
      return forbidden
        .filter((pattern) => pattern.test(source))
        .map((pattern) => `${relative(webRoot, path)}: ${pattern.source}`);
    });

    expect(offenders).toEqual([]);
  });

  it("subscribes with zero replay and only preserves live market behavior", () => {
    const provider = readFileSync(join(srcRoot, "shared/socket/IntelSocketProvider.tsx"), "utf8");

    expect(provider).toContain("replay: 0");
    expect(provider).not.toMatch(/\breplay:\s*[1-9]/);
    expect(provider).not.toContain('payload.type === "notification"');
    expect(provider).toContain('payload.type === "live_market_update"');
    expect(provider).toContain("registerMarketTargets");
  });
});

function collectSourceFiles(root: string): string[] {
  return readdirSync(root).flatMap((entry) => {
    const path = join(root, entry);
    if (statSync(path).isDirectory()) {
      return collectSourceFiles(path);
    }
    return sourceExtensions.has(extname(path)) ? [path] : [];
  });
}
