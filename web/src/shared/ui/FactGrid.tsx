import { cn } from "@lib/utils";
import type { ReactNode } from "react";

import "./FactGrid.css";

export type Fact = { label: string; value: ReactNode };

/**
 * Renders only the facts that have a value. Callers pass the full list and let empty entries fall out, so a
 * macro Event with no assets shows a shorter grid instead of a row of dashes.
 */
export function FactGrid({
  className,
  columns,
  facts,
  label,
}: {
  className?: string;
  /** Fix the column count when the cell count is known and a ragged last row would read as a gap. */
  columns?: number;
  facts: Fact[];
  label: string;
}) {
  const filled = facts.filter((fact) => Boolean(fact.value));
  if (!filled.length) return null;
  return (
    <dl
      aria-label={label}
      className={cn("ui-fact-grid", className)}
      style={columns ? ({ "--ui-fact-columns": columns } as React.CSSProperties) : undefined}
    >
      {filled.map((fact) => (
        <div className="ui-fact" key={fact.label}>
          <dt>{fact.label}</dt>
          <dd>{fact.value}</dd>
        </div>
      ))}
    </dl>
  );
}
