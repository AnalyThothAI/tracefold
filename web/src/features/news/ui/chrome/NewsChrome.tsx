import { cn } from "@lib/utils";
import type { ReactNode } from "react";

import "./newsChrome.css";

/**
 * The frame every News route shares. Each route owns its own content and its own stylesheet; this owns the
 * reading measure, the heading block, the folded technical-evidence disclosure and the "nothing here"
 * sentence, so the four routes cannot drift apart on the parts a reader recognises as "the same console".
 *
 * The archetype sets the measure and nothing else: `scan` is a wide list surface, `case` is one document and
 * is centred at a reading width rather than hugging the frame.
 */
export function NewsPageShell({
  archetype,
  children,
  className,
  label,
}: {
  archetype: "case" | "scan";
  children: ReactNode;
  className: string;
  label: string;
}) {
  return (
    <section
      aria-label={label}
      className={cn("news-panel", className)}
      data-page-archetype={archetype}
    >
      {children}
    </section>
  );
}

/**
 * Title and subtitle share a baseline rather than stacking: the subtitle is a caption on the title, not a
 * second heading, and stacking them cost the first row of every list a line of height.
 */
export function NewsPageHeader({
  children,
  subtitle,
  title,
}: {
  children?: ReactNode;
  subtitle?: string;
  title: string;
}) {
  return (
    <header className="news-page-header">
      <div className="news-heading-copy">
        <h1>{title}</h1>
        {subtitle ? <p>{subtitle}</p> : null}
      </div>
      {children ? <div className="news-heading-aside">{children}</div> : null}
    </header>
  );
}

/** When the page's own numbers were measured. Monospace so it does not twitch as the seconds tick. */
export function NewsPageStamp({ children }: { children: ReactNode }) {
  return <span className="news-page-stamp">{children}</span>;
}

/** Internal identifiers and raw records: present, replayable, and folded away from the reading surface. */
export function NewsTechnical({ children, summary }: { children: ReactNode; summary: string }) {
  return (
    <details className="news-technical">
      <summary>{summary}</summary>
      <div>{children}</div>
    </details>
  );
}

export function NewsEmptyNote({ children }: { children: ReactNode }) {
  return <p className="news-empty-note">{children}</p>;
}
