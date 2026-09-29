import type { TradingExecutionRow } from "../api/tradingQueries";

/** Count each venue entry once from the durable execution projection. */
export function entrySplit(executions: readonly TradingExecutionRow[]): {
  accepted: number;
  closed: number;
  filled: number;
  refused: number;
} {
  const refused = executions.filter((row) => row.stage === "rejected" || row.stage === "expired");
  return {
    accepted: executions.filter((row) =>
      ["ordered", "filled", "protected", "closed"].includes(row.stage),
    ).length,
    closed: executions.filter((row) => row.stage === "closed").length,
    filled: executions.filter((row) => row.fill_quantity != null).length,
    refused: refused.length,
  };
}
