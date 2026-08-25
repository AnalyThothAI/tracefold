import type { NewsStatus } from "../../api/newsQueries";
import { absoluteTime, healthLevelLabel, healthTone } from "../../model/newsLabels";

import { NewsToneDot } from "./NewsTone";

import "./newsHealthPill.css";

/*
 * The feed used to carry its own health pill here. #207 moved that read to the topbar lamp: the same
 * "silent while ok" rule, but present on every route rather than one, and reaching the two narrower frames
 * the sidebar's health dot never did. What is left in this file is the status route's own header pill.
 */

/** The status route's own header pill: the full read, always shown, plus when it was measured. */
export function NewsOverallPill({ status }: { status: NewsStatus }) {
  const level = status.health.overall;
  return (
    <span className="news-health-pill news-toned" data-tone={healthTone(level)} role="status">
      <NewsToneDot />
      <b>总体{healthLevelLabel(level)}</b>
      <span className="news-health-pill-time">
        更新于 {absoluteTime(status.measured_at_ms).slice(11)}
      </span>
    </span>
  );
}
