import type { NewsOutcome } from "../../api/newsQueries";
import { outcomeTone } from "../../model/newsLabels";

import { NewsToneDot } from "./NewsTone";

import "./newsOutcome.css";

/**
 * The single reader-facing conclusion for one Event. `outcome.text_zh` is the badge; `reason_zh` is the
 * one-line why (rendered inline in `detailed` mode, as a tooltip title otherwise).
 *
 * `emphasis="solid"` is reserved for the high-priority push — the caller decides from `priority`, because
 * `⚡` is a transport decision the server already made and the browser only renders it louder.
 */
export function NewsOutcomeBadge({
  detailed = false,
  emphasis = "soft",
  outcome,
  size = "md",
}: {
  detailed?: boolean;
  emphasis?: "soft" | "solid";
  outcome: NewsOutcome;
  size?: "md" | "lg";
}) {
  const tone = outcomeTone(outcome.kind);
  const pending = outcome.group === "pending";
  return (
    <span
      className="news-outcome news-toned"
      data-emphasis={emphasis === "solid" ? "solid" : undefined}
      data-outcome={outcome.kind}
      data-size={size}
      data-tone={tone}
      title={outcome.reason_zh || undefined}
    >
      <NewsToneDot halo={tone !== "neutral"} pulse={pending} size={size === "lg" ? "lg" : "md"} />
      <span className="news-outcome-text">{outcome.text_zh}</span>
      {detailed && outcome.reason_zh ? (
        <span className="news-outcome-reason">{outcome.reason_zh}</span>
      ) : null}
    </span>
  );
}
