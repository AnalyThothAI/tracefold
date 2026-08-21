import { dayBucketLabel, hourBucketKey, hourBucketLabel } from "@features/news/model/newsLabels";
import { describe, expect, it } from "vitest";

/**
 * The feed's hour headings (design proposal ⑤). They are presentational, but they make a claim about *when*
 * the rows under them are, and the feed window reaches 72 h with 加载更多 walking further back — so the one
 * thing they must not do is let two different days wear the same heading.
 */
describe("hour buckets", () => {
  const at = (iso: string) => new Date(iso).getTime();

  it("buckets by the local hour, and the same hour on two days is two buckets", () => {
    expect(hourBucketKey(at("2026-08-21T03:05:00+08:00"))).toBe(
      hourBucketKey(at("2026-08-21T03:59:00+08:00")),
    );
    expect(hourBucketKey(at("2026-08-21T03:05:00+08:00"))).not.toBe(
      hourBucketKey(at("2026-08-20T03:05:00+08:00")),
    );
  });

  it("labels the span, and carries the date when the caller says the day changed", () => {
    expect(hourBucketLabel(at("2026-08-21T03:05:00+08:00"))).toBe("03:00 — 04:00");
    expect(hourBucketLabel(at("2026-08-21T03:05:00+08:00"), true)).toBe("08-21 03:00 — 04:00");
    // Midnight wraps rather than reading `23:00 — 24:00`.
    expect(hourBucketLabel(at("2026-08-21T23:30:00+08:00"))).toBe("23:00 — 00:00");
  });

  it("names the day the same way the review queue does", () => {
    expect(dayBucketLabel(at("2026-08-21T03:05:00+08:00"))).toBe("08-21");
  });
});
