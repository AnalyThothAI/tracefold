import type { ReactNode } from "react";

import "./newsChrome.css";

/*
 * The News-owned parts of the frame every News route shares: the heading block, the measured-at stamp and
 * the folded technical-evidence disclosure. The measure the route sits in and its "nothing here" sentence
 * are the console's, not News's — `@shared/ui/PageShell` and `@shared/ui/EmptyNote` since #589 PR-5.
 */

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
  /* A node rather than a string: two surfaces name their sibling in the caption, and a cross-link is the
     shortest honest way to say "that question is answered over there" (#256). */
  subtitle?: ReactNode;
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
