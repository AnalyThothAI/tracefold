import type { ReactNode } from "react";

/**
 * Where the numbers above came from, in the endpoint's own words (#207 principle 2).
 *
 * Every panel on this page closes with one of these. It is not decoration: a figure whose provenance cannot
 * be written as `GET /api/… → field` is a figure the browser derived, and this line is what makes that
 * impossible to hide. `note` is for what the panel deliberately does *not* show.
 */
export function NewsOiSource({ note, path }: { note?: ReactNode; path: string }) {
  return (
    <p className="news-oi-source">
      <span className="news-oi-source-label">读自</span>
      <code>{path}</code>
      {note ? <span className="news-oi-source-note">{note}</span> : null}
    </p>
  );
}
