import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import { extname, join, relative } from "node:path";

import { describe, expect, it } from "vitest";

const srcRoot = join(process.cwd(), "src");
const sourceExtensions = new Set([".css", ".ts", ".tsx"]);

/**
 * #82's keyboard layer was cut whole: no ⌘K palette, no `?` panel, no reading cursor, no `G` chord, no digit
 * tab keys, no `<kbd>` hints.
 *
 * Every action the palette collapsed is already a control on the page, so the layer only ever bought a second
 * way to reach what one click reached plus a command list that had to be kept in sync with the routes — the
 * feed toolbar was even advertising an `X 复制标注` binding that nothing implemented. What is left is the
 * platform's own keyboard: real controls in tab order, `Enter` on a form, and Radix's `Esc` on the drawer.
 *
 * The `addEventListener("keydown")` ban is the load-bearing one. A React `onKeyDown` on a real control is
 * fine and still used (`Enter` applies the symbol filter); a document-level listener is how the layer grew.
 */
describe("console keyboard layer hard cut", () => {
  it("deletes the palette, the shortcut panel and the feed reading cursor", () => {
    for (const removed of [
      "shared/ui/CommandPalette.tsx",
      "shared/ui/CommandPalette.css",
      "shared/ui/ShortcutsDialog.tsx",
      "shared/ui/ShortcutsDialog.css",
      "features/cockpit/ui/appShortcuts.ts",
      "features/news/state/useFeedCursor.ts",
    ]) {
      expect(existsSync(join(srcRoot, removed))).toBe(false);
    }
  });

  it("keeps retired keyboard vocabulary out of production source", () => {
    const blocked = [
      'addEventListener("keydown"',
      "APP_SHORTCUTS",
      "CommandPalette",
      "ShortcutsDialog",
      "appShortcuts",
      "useFeedCursor",
      "data-cursor",
      "--surface-cursor",
      "kbd",
      "命令面板",
      "快捷键",
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
