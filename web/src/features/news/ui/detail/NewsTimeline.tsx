import { KeyValue, KeyValueRow } from "@shared/ui/KeyValue";
import { useState } from "react";

import type { NewsTimelineStep } from "../../api/newsQueries";
import { absoluteTime, clockTime, optionalDuration, timelineStageTone } from "../../model/newsLabels";
import { NewsEmptyNote } from "../chrome/NewsChrome";

import "./newsTimeline.css";

/**
 * The server's ordered steps, rendered as they arrived. Every sentence is `summary_zh`; the raw `facts` the
 * step was built from stay behind a toggle so the reading surface is prose and the evidence is one click away.
 */
export function NewsTimeline({ steps }: { steps: NewsTimelineStep[] }) {
  if (!steps.length) return <NewsEmptyNote>尚无处理记录。</NewsEmptyNote>;
  return (
    <ol className="news-timeline">
      {steps.map((step, index) => (
        <TimelineStep
          delta={index === 0 ? null : step.at_ms - steps[index - 1].at_ms}
          key={`${step.stage}-${index}`}
          last={index === steps.length - 1}
          step={step}
        />
      ))}
    </ol>
  );
}

function TimelineStep({
  delta,
  last,
  step,
}: {
  delta: number | null;
  last: boolean;
  step: NewsTimelineStep;
}) {
  const [open, setOpen] = useState(false);
  const facts = Object.entries(step.facts ?? {}).filter(([, value]) => !isEmptyFact(value));
  return (
    <li
      className="news-timeline-step news-toned"
      data-last={last || undefined}
      data-stage={step.stage}
      data-tone={timelineStageTone(step.stage)}
    >
      <span aria-hidden className="news-timeline-marker" />
      <div className="news-timeline-body">
        <div className="news-timeline-head">
          <b>{step.title_zh}</b>
          <time dateTime={new Date(step.at_ms).toISOString()} title={absoluteTime(step.at_ms)}>
            {clockTime(step.at_ms)}
          </time>
          {delta == null ? null : (
            <span className="news-timeline-delta">+{optionalDuration(delta)}</span>
          )}
        </div>
        <p className="news-timeline-summary">{step.summary_zh}</p>
        {facts.length ? (
          <button
            aria-expanded={open}
            className="news-timeline-toggle"
            onClick={() => setOpen((current) => !current)}
            type="button"
          >
            {open ? "收起字段" : `展开字段 (${facts.length})`}
          </button>
        ) : null}
        {open ? (
          <KeyValue className="news-timeline-fields">
            {facts.map(([key, value]) => (
              <KeyValueRow k={key} key={key} v={formatFact(value)} />
            ))}
          </KeyValue>
        ) : null}
      </div>
    </li>
  );
}

function isEmptyFact(value: unknown): boolean {
  if (value == null || value === "" || value === false) return true;
  if (Array.isArray(value)) return value.length === 0;
  if (typeof value === "object") return Object.keys(value as object).length === 0;
  return false;
}

function formatFact(value: unknown): string {
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return JSON.stringify(value);
}
