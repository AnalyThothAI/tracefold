import type { ReactNode } from "react";

import "./newsChrome.css";

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
