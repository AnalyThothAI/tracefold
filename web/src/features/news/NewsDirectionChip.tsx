import "./newsDirection.css";
import { directionGlyph, directionTone } from "./newsLabels";
import type { NewsTriageSummary } from "./useNewsPage";

/**
 * The model's market direction for one Event, paired with its magnitude. `direction_zh` and `magnitude_zh` are
 * server-owned copy; this component only picks the visual tone and the arrow that carries the same meaning
 * without colour.
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
      <span className="news-direction" data-size={size} data-tone={directionTone(triage.direction)}>
        <span aria-hidden className="news-direction-glyph">
          {directionGlyph(triage.direction)}
        </span>
        {triage.direction_zh}
      </span>
      {strength ? <span className="news-direction-strength">{strength}</span> : null}
    </span>
  );
}
