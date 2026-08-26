import type { NewsFeedEvent } from "../api/newsQueries";

import { dayBucketLabel, hourBucketKey, hourBucketLabel } from "./newsLabels";

export type NewsFeedGroup = {
  /** The local hour this run belongs to. Two runs of the same hour are two groups, not one. */
  bucket: string;
  events: NewsFeedEvent[];
  key: string;
  label: string;
};

/**
 * The feed's hour headings (#256).
 *
 * A real-time stream costs the reader any sense of *when* they are after two screens of scrolling, and the
 * v7 artifact answers that with a thin band every time the hour turns.
 *
 * Groups are consecutive runs, not a bucketed map. The server returns the page in its own order and this
 * function preserves it — a run that ends and an identical hour that starts again later are two headings,
 * because that is what actually arrived. The day prefix appears only when the loaded page spans more than
 * one, so a 24 h window keeps the artifact's bare `03:00 — 04:00` and a 168 h one cannot show two unrelated
 * `03:00` headings with nothing between them to say they are three days apart.
 *
 * The shape a group takes on screen is the caller's. This returns runs and their labels; whether the feed
 * ever makes one collapsible, or counts something else into it, is not decided here.
 */
export function newsFeedGroups(events: readonly NewsFeedEvent[]): NewsFeedGroup[] {
  const days = new Set(events.map((event) => dayBucketLabel(event.opened_at_ms)));
  const withDay = days.size > 1;
  const groups: NewsFeedGroup[] = [];
  for (const event of events) {
    const bucket = hourBucketKey(event.opened_at_ms);
    const last = groups[groups.length - 1];
    if (last && last.bucket === bucket) {
      last.events.push(event);
      continue;
    }
    groups.push({
      bucket,
      events: [event],
      // Two runs of the same hour must not collide as React keys; the first Event's id makes the run unique.
      key: `${bucket}:${event.event_id}`,
      label: hourBucketLabel(event.opened_at_ms, withDay),
    });
  }
  return groups;
}
