import { cn } from "@lib/utils";
import type { ComponentPropsWithoutRef, ReactNode } from "react";

import "./Card.css";

/**
 * The console's one panel shape: radius 10 on white, separated from the canvas by a 1px inset ring. Never a
 * drop shadow — on a light ground elevation is reserved for the four things that actually float.
 *
 * `flush` is for a panel whose body is a table or a list of rows: the header takes a rule beneath it and the
 * body loses its padding, so rows can run edge to edge and draw their own hairlines. Everything else is
 * padded and its header is just the first line of the card.
 */
export function Card({
  children,
  className,
  flush = false,
  hint,
  title,
  titleAs: TitleTag = "h2",
  titleStyle = "heading",
  ...props
}: ComponentPropsWithoutRef<"section"> & {
  flush?: boolean;
  hint?: ReactNode;
  title?: ReactNode;
  titleAs?: "h2" | "h3";
  /** `eyebrow` is for a card whose heading is a window label (`LAST 24H`) rather than a subject. */
  titleStyle?: "eyebrow" | "heading";
}) {
  return (
    <section className={cn("ui-card", className)} data-flush={flush || undefined} {...props}>
      {title ? (
        <div className="ui-card-header" data-title-style={titleStyle}>
          <TitleTag>{title}</TitleTag>
          {hint ? <small>{hint}</small> : null}
        </div>
      ) : null}
      <div className="ui-card-body">{children}</div>
    </section>
  );
}

/** The sentence a flush card closes with: what the table above it does or does not mean. */
export function CardNote({ children }: { children: ReactNode }) {
  return <p className="ui-card-note">{children}</p>;
}
