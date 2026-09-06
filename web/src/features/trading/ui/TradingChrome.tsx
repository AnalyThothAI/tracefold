import { EmptyNote } from "@shared/ui/EmptyNote";

import { ledgerSentence } from "../model/tradingLabels";

/**
 * The one sentence every ledger on the desk shows instead of rows (#537 PR-5).
 *
 * The frame, the provenance line and the bare note are the console's own primitives now — `PageShell`,
 * `SourceLine` and `EmptyNote` in `@shared/ui` (#589 PR-5). This is the only Trading-owned piece left here:
 * it is not a shape, it is the desk's wording for a ledger that answered but had nothing to list.
 */
export function TradingLedgerNote(props: { failed: boolean; pending: boolean; subject: string }) {
  return <EmptyNote className="trading-empty-note">{ledgerSentence(props)}</EmptyNote>;
}
