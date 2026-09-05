import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { formatChangePct, formatPrice } from "@features/news/model/newsPrice";
import { describe, expect, it } from "vitest";

/**
 * The console and the pushed card write the same number the same way (#562 PR-A).
 *
 * A reader sees a price twice: once on a Feishu or Telegram card, once in this console. Those two
 * surfaces have separate implementations — Python's `tracefold/news/card_format.py` and this
 * module — and the only thing holding them together used to be a comment asking the next editor to
 * remember. `tests/fixtures/news/card_money_format.json` is that agreement written down: this suite
 * and `tests/news/test_news_reader_card.py` assert the same table, so changing the rule on one
 * surface alone fails on the other.
 *
 * The table is read from the repository rather than duplicated here on purpose. A copy would drift
 * exactly the way the two implementations did.
 */
const repositoryRoot = join(dirname(fileURLToPath(import.meta.url)), "../../../../..");
const TABLE_PATH = join(repositoryRoot, "tests/fixtures/news/card_money_format.json");
const table = JSON.parse(readFileSync(TABLE_PATH, "utf8")) as {
  prices: { value: string; price: string }[];
  changes: { value: number; change: string }[];
};

describe("card and console money formatting", () => {
  it("reads a shared table that covers every branch of the rule", () => {
    expect(table.prices.length).toBeGreaterThanOrEqual(10);
    expect(table.changes.length).toBeGreaterThanOrEqual(8);
    // Thousands and two decimals from 1000 up, up to four below it, up to six below one.
    expect(table.prices.some((row) => Number(row.value) >= 1000)).toBe(true);
    expect(table.prices.some((row) => Number(row.value) >= 1 && Number(row.value) < 1000)).toBe(
      true,
    );
    expect(table.prices.some((row) => Number(row.value) < 1)).toBe(true);
    expect(table.changes.some((row) => row.value > 0)).toBe(true);
    expect(table.changes.some((row) => row.value < 0)).toBe(true);
  });

  it.each(table.prices)("formats $value as $price", ({ value, price }) => {
    expect(formatPrice(value)).toBe(price);
  });

  it.each(table.changes)("formats $value% as $change", ({ value, change }) => {
    expect(formatChangePct(value)).toBe(change);
  });

  /**
   * The one place the two surfaces answer differently, and deliberately. A table cell has a column to
   * keep, so it prints an em dash; a card drops the whole entry, because `行情 BTC $—` is worse than
   * no line at all (#88). The Python suite asserts the other half of this.
   */
  it("says an absent price in the console's own words", () => {
    expect(formatPrice(null)).toBe("—");
    expect(formatPrice("0")).toBe("—");
    expect(formatPrice("not-a-price")).toBe("—");
    expect(formatChangePct(null)).toBe("—");
  });
});
