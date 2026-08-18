import "./newsOutcome.css";
import { outcomeTone } from "./newsLabels";
import type { NewsOutcome } from "./useNewsPage";

/**
 * The single reader-facing conclusion for one Event. `outcome.text_zh` is the badge; `reason_zh` is the
 * one-line why (rendered inline in `detailed` mode, as a tooltip title otherwise).
 */
export function NewsOutcomeBadge({
  detailed = false,
  outcome,
  size = "md",
}: {
  detailed?: boolean;
  outcome: NewsOutcome;
  size?: "md" | "lg";
}) {
  const tone = outcomeTone(outcome.kind);
  return (
    <span
      className="news-outcome news-toned"
      data-outcome={outcome.kind}
      data-size={size}
      data-tone={tone}
      title={outcome.reason_zh || undefined}
    >
      <span aria-hidden className="news-outcome-dot" />
      <span className="news-outcome-text">{outcome.text_zh}</span>
      {detailed && outcome.reason_zh ? (
        <span className="news-outcome-reason">{outcome.reason_zh}</span>
      ) : null}
    </span>
  );
}
