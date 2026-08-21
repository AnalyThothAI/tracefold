import "./newsTone.css";

/**
 * The status light shared by the outcome word, the health pill and card, the task tabs, the reason groups and
 * the timeline. It reads the tone its `.news-toned` ancestor resolved, so a caller sets the tone once on the
 * container and every dot beneath it agrees.
 *
 * A 5px dot with no halo: at this size a ring around it reads as a second, larger dot.
 */
export function NewsToneDot({
  pulse = false,
  size = "md",
}: {
  pulse?: boolean;
  size?: "md" | "lg";
}) {
  return (
    <span
      aria-hidden
      className="news-tone-dot"
      data-pulse={pulse ? "on" : undefined}
      data-size={size === "lg" ? "lg" : undefined}
    />
  );
}
