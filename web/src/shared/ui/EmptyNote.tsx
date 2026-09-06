import { cn } from "@lib/utils";
import type { ReactNode } from "react";

import "./EmptyNote.css";

/**
 * The one sentence a section shows instead of rows — never an empty grid, a row of dashes, or "加载失败".
 * An empty ledger is an answer, and the sentence has to say which answer it is.
 *
 * `className` lets a feature set the note inside its own frame (the desk's ledgers pad it to the card);
 * it does not restyle `.empty-note`.
 */
export function EmptyNote({ children, className }: { children: ReactNode; className?: string }) {
  return <p className={cn("empty-note", className)}>{children}</p>;
}
