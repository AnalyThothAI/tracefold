import { cn } from "@lib/utils";
import type { ReactNode } from "react";

import "./PageShell.css";

/**
 * The measure a route surface sits in — width, gutters, and the vertical rhythm between its sections.
 *
 * One primitive since #589 PR-5: News and Trading each owned a private copy with the same seven
 * declarations, kept in step by a comment asking the next reader to keep them in step. `data-page-archetype`
 * is a structural hook the shell's own layout tests and the visual baselines read to know a route surface
 * has mounted; `scan` is a wide list surface, `case` is one document and is centred at a reading measure
 * rather than hugging the frame. `className` is the route's own hook for what only that route needs — a
 * feature may frame the shell, it may not restyle `.page-shell`.
 */
export function PageShell({
  archetype,
  children,
  className,
  label,
}: {
  archetype: "case" | "scan";
  children: ReactNode;
  className?: string;
  label: string;
}) {
  return (
    <section
      aria-label={label}
      className={cn("page-shell", className)}
      data-page-archetype={archetype}
    >
      {children}
    </section>
  );
}
