import { newsStatusPath } from "@shared/routing/paths";
import { ChevronRight } from "lucide-react";
import { Link } from "react-router-dom";

import type { NewsStatus } from "../../api/newsQueries";
import { absoluteTime, healthLevelLabel, healthTone } from "../../model/newsLabels";

import { NewsToneDot } from "./NewsTone";

import "./newsHealthPill.css";

const HEALTH_ITEMS = ["ingest", "broker", "model", "delivery"] as const;

/**
 * The feed's corner read on the pipeline: the server's overall level, the failing item's own sentence, and a
 * door to the status route. Healthy state is not silent here the way the topbar's is — the reader is already
 * looking at the feed this pipeline fills, so "正常" is the useful answer, not noise.
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
  if (!status || !health) {
    return (
      <span className="news-health-pill news-toned" data-tone="neutral" role="status">
        <NewsToneDot halo={false} />
        <b>正在检查流水线</b>
      </span>
    );
  }
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

/** The status route's own header pill: the same read, plus when it was measured. */
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
