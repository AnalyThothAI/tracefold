import { readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, join, relative, sep } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const webRoot = join(dirname(fileURLToPath(import.meta.url)), "../..");
const srcRoot = join(webRoot, "src");
const testsRoot = join(webRoot, "tests");

describe("frontend test placement", () => {
  it("keeps production src free of frontend test files", () => {
    const offenders = collectFiles(srcRoot)
      .filter((path) => /\.(test|spec)\.(ts|tsx)$/.test(path))
      .map((path) => relative(webRoot, path));

    expect(offenders).toEqual([]);
  });

  it("keeps production src free of frontend test and fixture folders", () => {
    const offenders = collectDirectories(srcRoot)
      .filter((path) => {
        const name = path.split(/[\\/]/).at(-1);

        return (
          name === "test" || name === "tests" || name === "fixtures" || name === "__fixtures__"
        );
      })
      .map((path) => relative(webRoot, path));

    expect(offenders).toEqual([]);
  });

  it("keeps route integration tests under tests/routes", () => {
    const offenders = collectFiles(testsRoot)
      .filter((path) => /\.test\.tsx$/.test(path))
      .filter((path) => readTest(path))
      .filter((path) => !relative(testsRoot, path).split(sep).join("/").startsWith("routes/"))
      .map((path) => relative(webRoot, path));

    expect(offenders).toEqual([]);
  });
});

function readTest(path: string): boolean {
  const text = readFileSync(path, "utf8");
  return /from\s+["'].*App["']|<App\s*\/>|renderAppRoute/.test(text);
}

function collectFiles(root: string): string[] {
  return readdirSync(root).flatMap((entry) => {
    const path = join(root, entry);
    return statSync(path).isDirectory() ? collectFiles(path) : [path];
  });
}

function collectDirectories(root: string): string[] {
  return readdirSync(root).flatMap((entry) => {
    const path = join(root, entry);

    if (!statSync(path).isDirectory()) {
      return [];
    }

    return [path, ...collectDirectories(path)];
  });
}
