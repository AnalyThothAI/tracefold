import { leverageRemaining, type LeverageCase } from "../../model/leverageCases";

import "./newsLeverageCard.css";

import { DECISION_LABEL, EVIDENCE_GLYPH } from "./leverageChrome";

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
  /* Read on every render, not memoised: the wall clock is the input, so a memo would either pin a stale
     `Date.now()` or list it as a dependency and never hit. */
  const remaining = leverageRemaining(item, Date.now());
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

      {/*
       * No phase chip here (#280). The artifact keeps 进行中 / 酝酿中 on the pane's header, where it
       * qualifies the one case being read; on a card it competed with the decision chip a few pixels
       * above it, and the two together made a five-line summary carry three verdict-shaped badges.
       */}
      <span className="news-leverage-card-tags">
        <span className="news-leverage-regime">{item.regime}</span>
        <code>{item.strategyLabel}</code>
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
        {/*
         * How long this case still has, from the ledger's own `must_close_at_ms` (#280). Age alone said
         * when it started and never when it ends, which on a lane whose whole risk control is a forced
         * close is the half a reader actually needs.
         */}
        <small className="news-leverage-card-remain">{remaining}</small>
        <small data-capital={item.capital ?? undefined}>
          {item.capital ? `资本 ${item.capital}` : "资本 —"}
        </small>
      </span>
    </button>
  );
}
