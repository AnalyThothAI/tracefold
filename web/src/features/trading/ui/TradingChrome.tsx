import type { ReactNode } from "react";

import "./tradingShell.css";

/**
 * The capital lane's frame and provenance line.
 *
 * Owned here rather than imported from News: the two features are siblings that must not reach into each
 * other's internals, and a shared frame is exactly the kind of import the boundary gate blocks. The shapes
 * match the News shell on purpose — a reader should not have to learn a second console.
 */
export function TradingShell({ children, label }: { children: ReactNode; label: string }) {
  return (
    /*
     * `data-page-archetype` is a structural hook the shell's own layout tests and the visual baselines read
     * to know a route surface has mounted; it is not News styling, and dropping it made the workbench
     * baseline wait for an element that never arrived.
     */
    <section aria-label={label} className="trading-shell" data-page-archetype="scan">
      {children}
    </section>
  );
}

/** `GET /api/… → field`. A figure that cannot be written this way is one the browser derived (#207 §2). */
export function TradingSourceLine({ note, path }: { note?: ReactNode; path: string }) {
  return (
    <p className="trading-source-line">
      <span className="trading-source-line-label">读自</span>
      <code>{path}</code>
      {note ? <span className="trading-source-line-note">{note}</span> : null}
    </p>
  );
}

export function TradingInvariantLine({ children }: { children: ReactNode }) {
  return (
    <p className="trading-source-line">
      <span className="trading-source-line-label">不变量</span>
      <span className="trading-source-line-note">{children}</span>
    </p>
  );
}

/** The sentence a table shows instead of rows. Never "加载失败" — an empty ledger is an answer. */
export function TradingEmptyNote({ children }: { children: ReactNode }) {
  return <p className="trading-empty-note">{children}</p>;
}
