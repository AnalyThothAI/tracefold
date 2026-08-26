import type { LeverageCase } from "../../model/leverageCases";

import "./newsLeverageCard.css";

import { DECISION_LABEL, EVIDENCE_GLYPH, PHASE_LABEL } from "./leverageChrome";

/**
 * One case in the list: what was decided, on what, and where the money stands.
 *
 * The card is a `button`, not a row with a click handler: selecting a case changes what the pane beside it
 * shows and is reversible, which is a control rather than a navigation. Its accessible name carries the
 * symbol and the decision, so a screen reader hears what it is selecting rather than "button".
 */
export function NewsLeverageCard({
  item,
  onSelect,
  selected,
}: {
  item: LeverageCase;
  onSelect: () => void;
  selected: boolean;
}) {
  return (
    <button
      aria-pressed={selected}
      className="news-leverage-card"
      data-decision={item.decision}
      data-selected={selected || undefined}
      onClick={onSelect}
      type="button"
    >
      <span className="news-leverage-card-head">
        <b>{item.base}</b>
        {/* The subject key, not a venue: the deterministic lane publishes no market for a frame, and a
            guessed one on a page about money is worse than none. */}
        <small>{item.underlyingKey}</small>
        <span className="news-leverage-decision" data-decision={item.decision}>
          {DECISION_LABEL[item.decision].chip}
        </span>
      </span>

      <span className="news-leverage-card-tags">
        <span className="news-leverage-regime">{item.regime}</span>
        <code>{item.strategyLabel}</code>
        <span className="news-leverage-phase" data-phase={item.phase}>
          {PHASE_LABEL[item.phase]}
        </span>
      </span>

      <span className="news-leverage-card-why">{item.why}</span>

      <span className="news-leverage-card-numbers">
        <code>{item.numbers}</code>
        {/*
         * The evidence matrix compressed to one glyph per row. It is a preview of the matrix in the pane,
         * never a score: four states, and 缺失 gets a dash rather than being left out.
         */}
        <span aria-hidden className="news-leverage-marks">
          {item.evidence.slice(0, 5).map((row) => (
            <small data-status={row.status} key={row.key}>
              {row.label}
              {EVIDENCE_GLYPH[row.status]}
            </small>
          ))}
        </span>
      </span>

      <span className="news-leverage-card-foot">
        <small>{item.age}</small>
        <small data-capital={item.capital ?? undefined}>
          {item.capital ? `资本 ${item.capital}` : "资本 —"}
        </small>
      </span>
    </button>
  );
}
