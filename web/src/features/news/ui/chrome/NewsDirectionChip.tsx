import type { NewsLegacyVerdict } from "../../api/newsQueries";
import { directionGlyph, directionTone } from "../../model/newsLabels";

import "./newsDirection.css";

/**
 * The model's market direction for one Event, paired with the kind of fact the card states.
 *
 * Set as coloured *text*, never a filled block: red and green are the only two hues on the page that mean
 * something about the market, and a solid one at the head of a meta line out-shouts the headline it belongs
 * to. The arrow carries the same meaning without colour — the two hues sit at near-equal luminance by
 * necessity, since both have to clear 4.5:1 on white.
 *
 * `direction_zh` and `fact_kind_zh` are server-owned copy; this only picks the tone and the glyph. A
 * verdict written before `news_judgment_v3` carries no fact kind, and the second span is simply absent.
 * Only a legacy verdict has a direction (#706): an EventUpdate keeps each claim's own reading and never
 * collapses them into one market call, so a News Agent Event renders no chip.
 */
export function NewsDirectionChip({
  size = "sm",
  verdict,
  withStrength = true,
}: {
  size?: "sm" | "lg";
  verdict: NewsLegacyVerdict;
  withStrength?: boolean;
}) {
  if (!verdict.direction_zh) return null;
  const strength = withStrength ? verdict.fact_kind_zh : "";
  return (
    <span className="news-direction-pair">
      <span className="news-direction" data-dir={directionTone(verdict.direction)} data-size={size}>
        <span aria-hidden className="news-direction-glyph">
          {directionGlyph(verdict.direction)}
        </span>
        {verdict.direction_zh}
      </span>
      {strength ? <span className="news-direction-strength">{strength}</span> : null}
    </span>
  );
}
