import type { ReactNode } from "react";

import "./SourceLine.css";

/**
 * Where the numbers above came from, in the endpoint's own words (#207 principle 2).
 *
 * Every panel that shows figures closes with one of these. It is not decoration: a figure whose provenance
 * cannot be written as `GET /api/… → field` is a figure the browser derived, and this line is what makes
 * that impossible to hide. `note` is for what the panel deliberately does *not* show.
 */
export function SourceLine({ note, path }: { note?: ReactNode; path: string }) {
  return (
    <p className="source-line">
      <span className="source-line-label">读自</span>
      <code>{path}</code>
      {note ? <span className="source-line-note">{note}</span> : null}
    </p>
  );
}
