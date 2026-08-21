import { cn } from "@lib/utils";
import type { ComponentPropsWithoutRef } from "react";

import "./ActionButton.css";

export type ActionButtonVariant =
  | "primary"
  | "secondary"
  | "quiet"
  | "positive"
  | "negative"
  | "caution";

/**
 * The console's one button shape.
 *
 * A screen gets at most one `primary`: it is the accent colour, and the accent marks the pipeline, so two of
 * them on a page means neither is the action. `secondary` is the default — white with a hairline ring, which
 * is what almost every control here is. `positive` / `negative` are the operator-label pair and use a 9%
 * wash rather than a filled red or green, because those two hues belong to market direction and a solid one
 * beside a headline would read as a call on the market.
 *
 * Every variant is at least 30px tall, and 44px below the tablet breakpoint where a thumb has to find it.
 */
export function ActionButton({
  className,
  size = "md",
  type = "button",
  variant = "secondary",
  ...props
}: ComponentPropsWithoutRef<"button"> & {
  size?: "sm" | "md" | "lg";
  variant?: ActionButtonVariant;
}) {
  return (
    <button
      className={cn("ui-button", className)}
      data-size={size}
      data-variant={variant}
      type={type}
      {...props}
    />
  );
}
