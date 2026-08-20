import { cn } from "@lib/utils";
import type { ReactNode } from "react";

import "./newsChrome.css";

/**
 * The frame every News route shares. Each route owns its own content and its own stylesheet; this owns the
 * panel, the heading block, the folded technical-evidence disclosure, and the "nothing here" sentence, so the
 * three routes cannot drift apart on the parts a reader recognises as "the same console".
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
      {children}
    </header>
  );
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
