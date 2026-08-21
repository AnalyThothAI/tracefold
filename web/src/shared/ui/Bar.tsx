import "./Bar.css";

export type BarTone = "accent" | "caution" | "neutral" | "bullish" | "bearish";

/**
 * A proportion, drawn once. Bars default to neutral grey and only take a colour when the row is being named:
 * indigo for the layer that made it through, amber for the one being pointed at. A page where every bar is
 * coloured has told the reader nothing about which one to look at.
 *
 * `share` is a percentage the caller computed from two server numbers; the bar clamps it into the track and
 * keeps a hairline of fill at zero so an empty row still reads as a row.
 */
export function Bar({
  share,
  size = "md",
  tone = "neutral",
}: {
  share: number;
  size?: "sm" | "md" | "lg";
  tone?: BarTone;
}) {
  return (
    <span aria-hidden className="ui-bar" data-size={size} data-tone={tone}>
      <span style={{ width: `${Math.max(0, Math.min(100, share))}%` }} />
    </span>
  );
}
