import { cn } from "@lib/utils";
import type { ReactNode } from "react";
import { Link } from "react-router-dom";

import "./Metric.css";

export type MetricTone = "accent" | "caution" | "plain";

/**
 * One figure, three lines, always in the same order: a Latin monospace eyebrow, the number, a Chinese caption.
 *
 * The eyebrow is Latin because the number is the content and a Chinese label at this size would compete with
 * it; the caption carries the meaning in the language the console is read in. Values are monospace with
 * tabular numerals so a figure that changes on a poll never shifts the ones beside it, and the eyebrow is
 * capped at seven characters for the same reason — a wrapped label breaks the row's alignment.
 */
export function Metric({
  caption,
  eyebrow,
  note,
  size = "md",
  title,
  to,
  tone = "plain",
  value,
}: {
  caption?: ReactNode;
  eyebrow: string;
  note?: ReactNode;
  size?: "sm" | "md";
  title?: string;
  to?: string | null;
  tone?: MetricTone;
  value: ReactNode;
}) {
  const body = (
    <>
      <span className="ui-metric-eyebrow">{eyebrow}</span>
      <b className="ui-metric-value">{value}</b>
      {caption || note ? (
        <span className="ui-metric-foot">
          {caption ? <small className="ui-metric-caption">{caption}</small> : null}
          {note ? <small className="ui-metric-note">{note}</small> : null}
        </span>
      ) : null}
    </>
  );
  const props = { className: "ui-metric", "data-size": size, "data-tone": tone, title };
  return to ? (
    <Link {...props} to={to}>
      {body}
    </Link>
  ) : (
    <span {...props}>{body}</span>
  );
}

/**
 * A row of metrics separated by the grid gap itself, so the hairlines land between cells at any count and
 * never leave a stray rule at the edge when the row wraps on a narrow screen.
 */
export function MetricRow({
  children,
  className,
  columns,
  label,
}: {
  children: ReactNode;
  className?: string;
  columns: number;
  label?: string;
}) {
  return (
    <div
      aria-label={label}
      className={cn("ui-metric-row", className)}
      style={{ "--ui-metric-columns": columns } as React.CSSProperties}
    >
      {children}
    </div>
  );
}
