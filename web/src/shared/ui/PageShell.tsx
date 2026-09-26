import { cn } from "@lib/utils";
import type { ReactNode } from "react";

import "./PageShell.css";

/**
 * The measure a route surface sits in — width, gutters, and the vertical rhythm between its sections.
 *
 * Every route keeps the same outer width and gutters, including loading and error states. `scan` and
 * `case` describe the content rather than changing its outer geometry. A case can use PageReadingContent
 * for a narrower document. Route classes own their inner layout, never this element's width or padding.
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

/** A document's reading measure inside the shared page boundary. */
export function PageReadingContent({ children }: { children: ReactNode }) {
  return <div className="page-reading-content">{children}</div>;
}
