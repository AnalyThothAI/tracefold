"""Give already-judged telemetry Events the assets their Gate could not ground (#267).

`news_event_assets` is where four planes ask "which assets does this Event concern": the Reaction
planner's due scan, the feed's `?symbol=` filter behind the token page, the instrument-grounding
funnel, and reader history's canonical-asset overlap. The deterministic lanes were absent from all
four, because an OI frame's wire text is `NVDA OI Rise 4.55%, OI Value 32.17M, …` and the admission
Gate grounds nothing in it — `grounded_assets` is `[]`, and the row that would have been written from
it never existed. In production that was 112 of 112 frames in a day, and the visible half of it was a
frame table whose 价格 / 1H / 4H columns were empty for every row that has ever existed.

The code now records the deterministic judge's own primaries when it settles the verdict. This
backfill establishes nothing that code would not have written: it reads the *persisted verdict* of
each already-judged deterministic Event and takes the same primaries, with the same normalization and
the same Event anchor. A frame that failed the template match carries `assets: []` and is correctly
left alone — there is no symbol to measure a price against.

Reactions are not seeded here. `REACTION_HISTORY_MAX_AGE_MS` is 30 days and the planner walks
Event-assets by anchor age, so it picks these up on its own turn and writes either real price points
or a named `unavailable` — `instrument_unresolved` for the stock perpetuals this universe does not
list, which is the honest answer rather than a hole.

That does hand the planner a one-off backlog, and its size is worth stating rather than discovering:
at this revision production holds 453 deterministic Events, 346 of them carrying a primary, and every
one is inside the 30-day reaction window. The due scan takes `REACTION_DUE_BATCH = 100` per turn and
walks oldest-first, so it drains in a handful of turns — during which `oldest_due_age_ms` reports the
oldest backfilled anchor and the Price-Review backlog SLO reads late. That is a true statement about
work that genuinely is outstanding, and it ends when the queue does.

Revision ID: 20260827_0314
Revises: 20260827_0313
"""

from __future__ import annotations

from alembic import op

revision = "20260827_0314"
down_revision = "20260827_0313"
branch_labels = None
depends_on = None

# The same two transformations `EventStorage.record_event_assets` applies, in the same order: upper,
# then strip a *leading* `XYZ-`. `replace(…, 'XYZ-', '')` would also strip the sequence from the middle
# of a tag, which is not what the running code does, and a backfilled row that the code would have
# written differently is exactly the drift this migration must not introduce.
_SYMBOL = "regexp_replace(upper(btrim(x ->> 'symbol')), '^XYZ-', '')"


def upgrade() -> None:
    op.execute(
        f"""
        INSERT INTO news_event_assets (symbol, event_id, market_type, opened_at_ms)
        SELECT DISTINCT {_SYMBOL}, e.event_id, left(x ->> 'market_type', 16), e.opened_at_ms
          FROM news_events e
          -- The newest triage verdict per Event, exactly as `due_reactions` reads one for `is_primary`.
          -- A redelivery settles the same Event again and both rows carry the same primaries, so this
          -- only keeps the migration from multiplying work.
          JOIN LATERAL (
            SELECT v.verdict
              FROM news_verdicts v
             WHERE v.event_id = e.event_id
               AND v.stage = 'triage'
               AND v.editorial ->> 'editorial_origin' = 'telemetry_deterministic'
             ORDER BY v.created_at_ms DESC
             LIMIT 1
          ) t ON true,
          LATERAL jsonb_array_elements(COALESCE(t.verdict -> 'assets', '[]'::jsonb)) x
         WHERE e.admission IN ('telemetry_deterministic', 'liquidation_deterministic')
           AND x ->> 'role' = 'primary'
           AND {_SYMBOL} <> ''
        ON CONFLICT DO NOTHING
        """
    )


def downgrade() -> None:
    # Deliberately not a `DELETE`. These rows are indistinguishable from the ones the running code
    # writes for every frame judged after this revision, so a downgrade that removed "the backfilled
    # ones" would have to guess, and guessing wrong deletes a live Event's price anchor.
    raise RuntimeError("20260827_0314 backfills Event assets that the running code also writes")
