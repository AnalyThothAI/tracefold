import type { NewsTriageSummary } from "../../api/newsQueries";
import { directionGlyph, directionTone } from "../../model/newsLabels";

import "./newsDirection.css";

/**
 * The model's market direction for one Event, paired with its magnitude.
 *
 * Set as coloured *text*, never a filled block: red and green are the only two hues on the page that mean
 * something about the market, and a solid one at the head of a meta line out-shouts the headline it belongs
 * to. The arrow carries the same meaning without colour — the two hues sit at near-equal luminance by
 * necessity, since both have to clear 4.5:1 on white.
 *
 * `direction_zh` and `magnitude_zh` are server-owned copy; this only picks the tone and the glyph.
 */
export function NewsDirectionChip({
  size = "sm",
  triage,
  withStrength = true,
}: {
  size?: "sm" | "lg";
  triage: NewsTriageSummary;
  withStrength?: boolean;
}) {
  if (!triage.direction_zh) return null;
  const strength = withStrength ? triage.magnitude_zh : "";
  return (
    <span className="news-direction-pair">
      <span className="news-direction" data-dir={directionTone(triage.direction)} data-size={size}>
        <span aria-hidden className="news-direction-glyph">
          {directionGlyph(triage.direction)}
        </span>
        {triage.direction_zh}
      </span>
      {strength ? <span className="news-direction-strength">{strength}</span> : null}
    </span>
  );
}
