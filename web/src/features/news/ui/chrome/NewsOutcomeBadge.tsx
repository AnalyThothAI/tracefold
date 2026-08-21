import type { NewsOutcome } from "../../api/newsQueries";
import { outcomeTone } from "../../model/newsLabels";

import { NewsToneDot } from "./NewsTone";

import "./newsOutcome.css";

/**
 * The single reader-facing conclusion for one Event, in two shapes.
 *
 * `text` is a dot and a word, and it is what a list uses: a capsule on every row draws a vertical band of
 * identical pills down the page and the reader stops seeing any of them. `chip` is the bordered capsule, and
 * it is reserved for the places where there is exactly one — the detail hero and the phone card.
 *
 * `outcome.text_zh` is the badge and `reason_zh` is the one-line why (inline in `detailed` mode, a tooltip
 * otherwise). Both arrive decided from the server; nothing here maps a rule key to copy.
 */
export function NewsOutcomeBadge({
  detailed = false,
  outcome,
  size = "md",
  variant = "text",
}: {
  detailed?: boolean;
  outcome: NewsOutcome;
  size?: "md" | "lg";
  variant?: "chip" | "text";
}) {
  const tone = outcomeTone(outcome.kind);
  return (
    <span
      className="news-outcome news-toned"
      data-outcome={outcome.kind}
      data-size={size}
      data-tone={tone}
      data-variant={variant}
      title={detailed ? undefined : outcome.reason_zh || undefined}
    >
      <NewsToneDot pulse={outcome.group === "pending"} size={size === "lg" ? "lg" : "md"} />
      <span className="news-outcome-text">{outcome.text_zh}</span>
      {detailed && outcome.reason_zh ? (
        <span className="news-outcome-reason">{outcome.reason_zh}</span>
      ) : null}
    </span>
  );
}
