import { readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, extname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const webRoot = join(dirname(fileURLToPath(import.meta.url)), "../..");
const srcRoot = join(webRoot, "src");
const sourceExtensions = new Set([".ts", ".tsx"]);
const featureRoot = join(srcRoot, "features");
const featureNames = featureDirectories();
const featureNamePattern = `(${featureNames.map(escapeRegExp).join("|")})`;

describe("feature boundaries", () => {
  it("does not import another feature internals by relative path", () => {
    const offenders = collectFiles(join(srcRoot, "features"))
      .filter((path) => sourceExtensions.has(extname(path)))
      .flatMap((path) => {
        const relativePath = relative(webRoot, path);
        const text = readFileSync(path, "utf8");
        const matches = [
          ...text.matchAll(
            new RegExp(`(?:\\.\\./)+${featureNamePattern}/(?:api|model|state|ui)(?:/|["'])`, "g"),
          ),
        ];
        return matches.map((match) => `${relativePath}: ${match[0]}`);
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

function featureDirectories(): string[] {
  return readdirSync(featureRoot)
    .filter((entry) => statSync(join(featureRoot, entry)).isDirectory())
    .sort();
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}
