import { readdirSync, readFileSync, statSync } from "node:fs";
import { basename, dirname, extname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";

import postcss from "postcss";
import { describe, expect, it } from "vitest";

const webRoot = join(dirname(fileURLToPath(import.meta.url)), "../..");
const srcRoot = join(webRoot, "src");

describe("responsive CSS contract", () => {
  it("requires every app-owned side-effect stylesheet to declare its cascade layer", () => {
    const offenders = collectFiles(srcRoot)
      .filter(isSideEffectCssFile)
      .filter((path) => !hasAppLayerDeclaration(readFileSync(path, "utf8")))
      .filter((path) => !isTailwindImportStylesheet(path))
      .map(
        (path) =>
          `${relativeToSrc(
            path,
          )} is unlayered; app-owned side-effect CSS must name an app cascade layer.`,
      );

    expect(offenders).toEqual([]);
  });
});

function collectFiles(root: string): string[] {
  return readdirSync(root).flatMap((entry) => {
    const path = join(root, entry);
    return statSync(path).isDirectory() ? collectFiles(path) : [path];
  });
}

function isCssFile(path: string): boolean {
  return extname(path) === ".css";
}

function isSideEffectCssFile(path: string): boolean {
  return isCssFile(path) && !basename(path).endsWith(".module.css");
}

function relativeToSrc(path: string): string {
  return relative(srcRoot, path);
}

function isTailwindImportStylesheet(path: string): boolean {
  if (!relativeToSrc(path).startsWith("styles/")) return false;
  let importsTailwind = false;
  postcss.parse(readFileSync(path, "utf8")).walkAtRules("import", (rule) => {
    if (/^(?:url\(\s*)?["']?tailwindcss["']?(?:\s*\))?$/.test(rule.params.trim())) {
      importsTailwind = true;
    }
  });
  return importsTailwind;
}

function hasAppLayerDeclaration(css: string): boolean {
  let declared = false;
  postcss.parse(css).walkAtRules("layer", (rule) => {
    if (
      rule.params
        .split(",")
        .map((name) => name.trim())
        .some((name) => APP_LAYERS.has(name))
    ) {
      declared = true;
    }
  });
  return declared;
}

const APP_LAYERS = new Set([
  "app.base",
  "app.primitives",
  "app.shell",
  "app.features",
  "app.overrides",
]);
