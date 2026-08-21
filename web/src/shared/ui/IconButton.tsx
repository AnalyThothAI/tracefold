import { cn } from "@lib/utils";
import type { ComponentPropsWithoutRef, ReactNode } from "react";

import "./IconButton.css";

/**
 * A square control that carries only a glyph, so it must carry a name for anyone who cannot see the glyph —
 * `aria-label` is required by the type, not by review.
 */
export function IconButton({
  children,
  className,
  size = "md",
  type = "button",
  ...props
}: Omit<ComponentPropsWithoutRef<"button">, "aria-label"> & {
  "aria-label": string;
  children: ReactNode;
  size?: "sm" | "md";
}) {
  return (
    <button className={cn("ui-icon-button", className)} data-size={size} type={type} {...props}>
      {children}
    </button>
  );
}
