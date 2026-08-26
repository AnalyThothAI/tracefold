import { policyRuleZh } from "@features/trading";
import { ChevronDown } from "lucide-react";

import type { LeverageCase, LeverageListRow } from "../../model/leverageCases";

import { NewsLeverageCard } from "./NewsLeverageCard";

import "./newsLeverageGroupCard.css";

/**
 * Several cases that stopped on the same rule, as one counted row that opens (#269).
 *
 * `news_oi_alignment_v1` needs a News trigger and a fresh OI frame for the same issuer to meet inside one
 * scan window, which is structurally near-zero — so `oi_context_missing` is the lane's resting state
 * rather than an event, and the production list was 59 near-identical cards of it with the day's one OI
 * case somewhere in the middle. Collapsing them is not hiding: the count is the headline, the symbols are
 * on the row, and every case is one click away and still individually selectable.
 *
 * It summarises and never re-decides. The rule is the ledger's own `policy_reason`, translated by the
 * capital lane's own vocabulary — a group is a rendering of rows that already agree, not a new verdict
 * about them.
 */
export function NewsLeverageGroupCard({
  expanded,
  onSelect,
  onToggle,
  row,
  selectedId,
}: {
  expanded: boolean;
  onSelect: (item: LeverageCase) => void;
  onToggle: () => void;
  row: Extract<LeverageListRow, { kind: "group" }>;
  selectedId: string | undefined;
}) {
  const holdsSelection = row.items.some((item) => item.id === selectedId);
  return (
    <div className="news-leverage-group" data-expanded={expanded || undefined}>
      <button
        aria-expanded={expanded}
        className="news-leverage-group-head"
        data-selected={holdsSelection || undefined}
        onClick={onToggle}
        type="button"
      >
        <span className="news-leverage-group-count">{row.items.length}</span>
        <span className="news-leverage-group-text">
          <b>{policyRuleZh(row.rule)}</b>
          <small>{row.label}</small>
        </span>
        {/*
         * The issuers, in list order and bounded. Naming them is what keeps this a summary of specific
         * rows rather than a bucket: a reader looking for one symbol can see whether it is in here.
         */}
        <span className="news-leverage-group-symbols">
          {row.items
            .slice(0, 6)
            .map((item) => item.base)
            .join(" · ")}
          {row.items.length > 6 ? ` +${row.items.length - 6}` : ""}
        </span>
        <ChevronDown aria-hidden className="news-leverage-group-chevron" />
      </button>
      {expanded ? (
        <div className="news-leverage-group-items">
          {row.items.map((item) => (
            <NewsLeverageCard
              item={item}
              key={item.id}
              onSelect={() => onSelect(item)}
              selected={item.id === selectedId}
            />
          ))}
        </div>
      ) : null}
    </div>
  );
}
