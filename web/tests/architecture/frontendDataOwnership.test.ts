import { readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, extname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const webRoot = join(dirname(fileURLToPath(import.meta.url)), "../..");
const srcRoot = join(webRoot, "src");
const sourceExtensions = new Set([".ts", ".tsx"]);
const forbiddenServerStatePatterns = [
  /\buseQuery\b/,
  /\buseMutation\b/,
  /\buseInfiniteQuery\b/,
  /\bgetApi\b/,
  /\bpostApi\b/,
  /\bqueryClient\.set[A-Z]\w*\b/,
];

describe("frontend data ownership", () => {
  it("keeps route modules and presentational UI out of direct server-state ownership", () => {
    const offenders = dataOwnershipFiles().flatMap((path) => {
      const source = readFileSync(path, "utf8");
      return forbiddenServerStatePatterns
        .filter((pattern) => pattern.test(source))
        .map((pattern) => `${relative(webRoot, path)}: ${pattern.source}`);
    });

    expect(offenders).toEqual([]);
  });
});

function dataOwnershipFiles(): string[] {
  return [...collectSourceFiles(join(srcRoot, "routes")), ...featureUiFiles()];
}

function featureUiFiles(): string[] {
  return readdirSync(join(srcRoot, "features")).flatMap((featureName) => {
    const uiRoot = join(srcRoot, "features", featureName, "ui");
    return statSync(join(srcRoot, "features", featureName)).isDirectory() && existsDirectory(uiRoot)
      ? collectSourceFiles(uiRoot)
      : [];
  });
}

function collectSourceFiles(root: string): string[] {
  return readdirSync(root).flatMap((entry) => {
    const path = join(root, entry);
    if (statSync(path).isDirectory()) {
      return collectSourceFiles(path);
    }
    return sourceExtensions.has(extname(path)) ? [path] : [];
  });
}

function existsDirectory(path: string): boolean {
  try {
    return statSync(path).isDirectory();
  } catch {
    return false;
  }
}
