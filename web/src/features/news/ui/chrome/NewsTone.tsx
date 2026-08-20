import "./newsTone.css";

/**
 * The status light shared by the outcome badge, the health pill and card, the task tabs and the timeline. It
 * reads the tone its `.news-toned` ancestor resolved, so a caller sets the tone once on the container.
 */
export function NewsToneDot({
  halo = true,
  pulse = false,
  size = "md",
}: {
  halo?: boolean;
  pulse?: boolean;
  size?: "md" | "lg";
}) {
  return (
    <span
      aria-hidden
      className="news-tone-dot"
      data-halo={halo ? undefined : "off"}
      data-pulse={pulse ? "on" : undefined}
      data-size={size === "lg" ? "lg" : undefined}
    />
  );
}
