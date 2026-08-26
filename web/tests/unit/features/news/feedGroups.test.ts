import { newsFeedGroups } from "@features/news/model/feedGroups";
import { newsFeedEventFixture } from "@tests/fixtures/newsFixture";
import { describe, expect, it } from "vitest";

/** Local midnight of 2026-08-21, so the labels below do not move with the runner's timezone. */
function at(day: number, hour: number, minute = 0): number {
  return new Date(2026, 7, day, hour, minute).getTime();
}

function event(id: string, openedAtMs: number) {
  return newsFeedEventFixture({ event_id: id, opened_at_ms: openedAtMs });
}

describe("newsFeedGroups", () => {
  it("groups consecutive rows by local hour and counts each run", () => {
    const groups = newsFeedGroups([
      event("a", at(21, 3, 58)),
      event("b", at(21, 3, 5)),
      event("c", at(21, 2, 40)),
    ]);

    expect(groups.map((group) => group.label)).toEqual(["03:00 — 04:00", "02:00 — 03:00"]);
    expect(groups.map((group) => group.events.map((row) => row.event_id))).toEqual([
      ["a", "b"],
      ["c"],
    ]);
  });

  it("names the day once a loaded page spans more than one", () => {
    // 加载更多 walks back past midnight, and two bare `03:00 — 04:00` headings three days apart, with
    // nothing between them to say so, is worse than no heading at all.
    const groups = newsFeedGroups([event("a", at(21, 3, 5)), event("b", at(20, 3, 5))]);

    expect(groups.map((group) => group.label)).toEqual([
      "08-21 03:00 — 04:00",
      "08-20 03:00 — 04:00",
    ]);
  });

  it("keeps two runs of the same hour apart rather than merging them", () => {
    // The browser renders the server's order and never re-sorts it. A run that ends and an identical hour
    // that starts again later is two headings, because that is what actually arrived.
    const groups = newsFeedGroups([
      event("a", at(21, 3, 50)),
      event("b", at(21, 2, 10)),
      event("c", at(21, 3, 5)),
    ]);

    expect(groups).toHaveLength(3);
    expect(new Set(groups.map((group) => group.key)).size).toBe(3);
  });

  it("returns nothing for an empty page", () => {
    expect(newsFeedGroups([])).toEqual([]);
  });
});
