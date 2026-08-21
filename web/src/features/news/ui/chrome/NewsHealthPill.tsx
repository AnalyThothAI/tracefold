import { newsStatusPath } from "@shared/routing/paths";
import { ChevronRight } from "lucide-react";
import { Link } from "react-router-dom";

import type { NewsStatus } from "../../api/newsQueries";
import { absoluteTime, healthLevelLabel, healthTone } from "../../model/newsLabels";

import { NewsToneDot } from "./NewsTone";

import "./newsHealthPill.css";

const HEALTH_ITEMS = ["ingest", "broker", "model", "delivery"] as const;

/**
 * The feed's read on the pipeline behind it — but only when there is something to say.
 *
 * A healthy pipeline is silent here: the sidebar already carries a dot for the status destination, the status
 * route carries the full read, and a permanent green "正常" beside a feed is a light the reader learns to
 * stop seeing. When a level is `warn` or `bad` the pill appears with the failing item's own sentence and a
 * door to the page that explains it, which is the only moment it earns its place in the header.
 */
export function NewsHealthPill({ error, status }: { error: boolean; status?: NewsStatus }) {
  if (error) {
    return (
      <Link className="news-health-pill news-toned" data-tone="alert" to={newsStatusPath()}>
        <NewsToneDot />
        <b>状态暂不可用</b>
        <ChevronRight aria-hidden className="news-health-pill-chevron" />
      </Link>
    );
  }
  // `health` is required by the contract; the guard only covers the seconds of a rolling deploy where the
  // console is newer than the API, so the feed keeps rendering instead of throwing.
  const health = status?.health;
  if (!status || !health || health.overall === "ok") return null;
  const level = health.overall;
  const worst = HEALTH_ITEMS.map((key) => health[key]).find((item) => item.level === level);
  return (
    <Link
      aria-label="查看流水线状态"
      className="news-health-pill news-toned"
      data-tone={healthTone(level)}
      to={newsStatusPath()}
    >
      <NewsToneDot />
      <b>流水线{healthLevelLabel(level)}</b>
      <small>{worst?.summary_zh ?? ""}</small>
      <ChevronRight aria-hidden className="news-health-pill-chevron" />
    </Link>
  );
}

/** The status route's own header pill: the same read, always shown, plus when it was measured. */
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
