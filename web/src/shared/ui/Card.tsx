import { cn } from "@lib/utils";
import type { ComponentPropsWithoutRef, ReactNode } from "react";

import "./Card.css";

/**
 * A titled panel. `title` + `hint` render the standard header row so a caller never re-lays it out; pass
 * children only when the card is untitled.
 */
export function Card({
  children,
  className,
  hint,
  title,
  titleAs: TitleTag = "h2",
  ...props
}: ComponentPropsWithoutRef<"section"> & {
  hint?: ReactNode;
  title?: ReactNode;
  titleAs?: "h2" | "h3";
}) {
  return (
    <section className={cn("ui-card", className)} {...props}>
      {title ? (
        <div className="ui-card-header">
          <TitleTag>{title}</TitleTag>
          {hint ? <small>{hint}</small> : null}
        </div>
      ) : null}
      {children}
    </section>
  );
}
