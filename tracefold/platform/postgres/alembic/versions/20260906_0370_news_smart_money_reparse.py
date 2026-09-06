"""The backfilled smart-money records are parsed once with the production parser (#562).

Migration evidence:

- category: one bounded durable-data pass over a closed set of `news_items` rows -- it inserts the
  `news_market_smart_money` fact each of them proves and rewrites that Item's parse status. No
  column, index, constraint, view, trigger or function changes
- why_database_must_change: `20260905_0365` classified every retained market Item by Strategy id and
  marked it `raw / market_backfill_not_reparsed`, which was the honest state of a frame no parser had
  ever been run against -- Strategy 2026 had no template in this repository at the time and no table
  to put one in. Two things have happened since. `news_market_smart_money` exists, and #560 taught
  `parse_smart_money` the provider's own `K`/`M`/`B` on every dollar figure on the template. All 112
  of those Items are Strategy 2026 position reports; 111 of the titles now parse, and the twelfth
  is a `Withdraw` report that is not a position report at all.

  Their reason is therefore no longer true of any of them: for 111 it says a parse has not been
  attempted when one now succeeds, and for the `Withdraw` it says the same when the parser has an
  answer and the answer is a refusal. A reader of `/news/market` sees the cost directly -- an Item
  with no typed fact beside it is its own group, keyed `raw|smart_money|<item_id>`, so 112 reports
  from a handful of accounts are 112 one-record groups instead of the account groups §4.4 describes.

  A reparse is parse evidence and nothing more (#553 §3.1): the provider record, its stamps and its
  source identity are untouched, `market_notify_state` stays `historical`, and no track, delivery or
  trade is created. That is why this is a revision and not a replay -- a replay through admission
  would be a second observation of a record that was observed once.
- current_source_revision: 20260906_0369
- minimum_supported_source_revision: 20260906_0369
- lock_level_and_order: `ROW EXCLUSIVE` on `news_market_smart_money` for the inserts, then on
  `news_items` for the status updates, both by primary key. No `ACCESS EXCLUSIVE` is taken and no
  other table is read or written
- statement_timeout: 120s set locally by the revision
- lock_timeout: 5s set locally by the revision
- estimated_rows: 112 at the audit SHA -- `market_parse_error = 'market_backfill_not_reparsed'` is
  written by exactly one statement in `20260905_0365` and by nothing else, ever, so the set this
  reads is closed and this revision is the only thing that empties it. Expected result: 111 rows
  inserted and marked `parsed`, 1 (`Withdraw USDC`) left `raw` under the truthful reason
- estimated_bytes: 111 narrow rows in `news_market_smart_money` and 112 `news_items` updates. Tens
  of kilobytes
- rewrite_or_index_build: none. No DDL at all; the two group indexes those tables already carry are
  maintained by the writes
- preflight_and_maintenance_boundary: none beyond the ordinary deploy. Every writer that could race
  this one writes the same rows under the same keys -- a live replay of one of these records inserts
  the identical `source_key`, because the fact identity below is recomputed with the production
  extractor rather than invented -- so `ON CONFLICT DO NOTHING` is the whole coordination. `make up`
  stops Workers anyway
- archive_current_compatibility: every stored value the provider sent is kept exactly as it is. The
  reconstructed facts carry the original `event_at_ms` and `received_at_ms` and take the rebuild
  moment as `available_at_ms`, because that is genuinely the first instant any consumer could read
  them (#553 §3.3). `market_notify_state` is deliberately not touched: `20260905_0366` marked these
  Items `historical` and a parse does not put a months-old report back on a reader's to-do list
- role_and_grant_impact: none; the single `tracefold` login is unchanged
- failure_state: the transaction rolls back completely and every Item keeps the reason it has
- roll_forward_or_verified_backup_restore: `downgrade` is refused. Deleting the facts would delete
  the only structured record of those reports, and restoring `market_backfill_not_reparsed` would
  restate a claim that is false the moment this revision has run -- the parser has been run, and for
  the `Withdraw` report it answered. Roll forward with a new revision
- production_postgres_image:
  postgres:18-bookworm@sha256:1961f96e6029a02c3812d7cb329a3b03a3ac2bb067058dec17b0f5596aca9296

Revision ID: 20260906_0370
Revises: 20260906_0369
Create Date: 2026-09-06 00:00:00
"""

from __future__ import annotations

from alembic import op

# The production parser, imported rather than frozen into this file. `20260905_0365` froze the OI
# template on purpose, because it was reconstructing what the provider sent in 2026 and a later
# parser generation must not silently change that reconstruction. This revision is the opposite
# statement: these Items were never parsed at all, and what the operator asked for is exactly the
# answer today's parser gives. A copy of the regex here would be a second parser able to disagree
# with the one every live frame goes through -- including about the `K`/`M`/`B` suffix #560 fixed,
# which is the whole reason the reparse is worth doing.
from tracefold.news.events.facts import extract_fact_units
from tracefold.news.smart_money import RAW_REASON_TEMPLATE_UNMATCHED, parse_smart_money
from tracefold.news.source_contracts import MARKET_PROVIDER

revision = "20260906_0370"
down_revision = "20260906_0369"
branch_labels = None
depends_on = None

_BACKFILL_REASON = "market_backfill_not_reparsed"

# Deterministic order and tie-breaker. The bound is the predicate itself: `20260905_0365` is the only
# writer of this reason and it ran once, so the set is closed at the 112 rows measured in production
# and every row this pass touches leaves it.
_UNPARSED_SQL = """
    SELECT i.item_id,
           i.title,
           i.source_item_key,
           i.published_at_ms,
           i.observed_at_ms,
           i.first_ingest_mode,
           i.market_source_strategy_id,
           i.provider_metadata ->> 'source' AS provider_source,
           CASE WHEN jsonb_typeof(i.provider_params -> 'relatedAddress') = 'string'
                THEN i.provider_params ->> 'relatedAddress' END AS related_address
      FROM public.news_items i
     WHERE i.market_kind = 'smart_money'
       AND i.market_parse_status = 'raw'
       AND i.market_parse_error = %(reason)s
     ORDER BY i.observed_at_ms, i.item_id
"""

_INSERT_FACT_SQL = """
    INSERT INTO public.news_market_smart_money (
      source_key, item_id, fact_id, ingest_mode, provider, source_strategy_id,
      trader_label, account_address, source_venue, raw_instrument, symbol,
      action, position_side, reported_notional_usd, price, pnl_usd,
      event_at_ms, received_at_ms, available_at_ms, created_at_ms,
      parser_version, provider_record_identity, source_contract_version,
      notional_semantics, price_semantics, completeness_assumption
    ) VALUES (
      %(source_key)s, %(item_id)s, %(fact_id)s, %(ingest_mode)s, %(provider)s, %(source_strategy_id)s,
      %(trader_label)s, %(account_address)s, %(source_venue)s, %(raw_instrument)s, %(symbol)s,
      %(action)s, %(position_side)s, %(reported_notional_usd)s, %(price)s, %(pnl_usd)s,
      %(event_at_ms)s, %(received_at_ms)s, %(available_at_ms)s, %(created_at_ms)s,
      %(parser_version)s, %(provider_record_identity)s, %(source_contract_version)s,
      %(notional_semantics)s, %(price_semantics)s, %(completeness_assumption)s
    )
    ON CONFLICT DO NOTHING
"""


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '120s'")

    bind = op.get_bind()
    # The rebuild moment, from the database's own clock, so every fact this pass writes reports the
    # same first-available instant.
    now_ms = int(bind.exec_driver_sql("SELECT (extract(epoch FROM now()) * 1000)::bigint").scalar_one())
    for row in bind.exec_driver_sql(_UNPARSED_SQL, {"reason": _BACKFILL_REASON}).fetchall():
        title = str(row.title or "")
        fact = parse_smart_money(
            title,
            item_id=row.item_id,
            # The fact identity the live path computed for this record, recomputed from the same
            # extractor rather than invented: a Strategy 2026 report is one whole-item unit, and its
            # id is a function of the Item and the normalized title. That is what makes the
            # `source_key` below the one a replay of the same provider record would produce, and the
            # `ON CONFLICT` a real idempotency rather than a hope.
            fact_id=extract_fact_units(item_id=row.item_id, raw_text=title, fallback_title=title)[0].fact_id,
            source_strategy_id=str(row.market_source_strategy_id or ""),
            provider_source=str(row.provider_source or ""),
            related_address=row.related_address,
            event_at_ms=int(row.published_at_ms),
            received_at_ms=int(row.observed_at_ms),
            provider_record_identity=str(row.source_item_key),
        )
        if fact is None:
            # `Withdraw USDC` and anything else this template cannot turn into numbers. The Item stays
            # `raw` and keeps its original text; only the reason changes, from "no parser has run" to
            # the answer the parser actually gave.
            bind.exec_driver_sql(
                """
                UPDATE public.news_items
                   SET market_parse_error = %(reason)s
                 WHERE item_id = %(item_id)s
                """,
                {"reason": RAW_REASON_TEMPLATE_UNMATCHED, "item_id": row.item_id},
            )
            continue
        bind.exec_driver_sql(
            _INSERT_FACT_SQL,
            {
                "source_key": fact.source_key,
                "item_id": fact.item_id,
                "fact_id": fact.fact_id,
                "ingest_mode": row.first_ingest_mode,
                "provider": MARKET_PROVIDER,
                "source_strategy_id": fact.source_strategy_id,
                "trader_label": fact.trader_label,
                "account_address": fact.account_address,
                "source_venue": fact.source_venue,
                "raw_instrument": fact.raw_instrument,
                "symbol": fact.symbol,
                "action": fact.action,
                "position_side": fact.position_side,
                "reported_notional_usd": fact.reported_notional_usd,
                "price": fact.price,
                "pnl_usd": fact.pnl_usd,
                "event_at_ms": fact.event_at_ms,
                "received_at_ms": fact.received_at_ms,
                "available_at_ms": now_ms,
                "created_at_ms": now_ms,
                "parser_version": fact.parser_version,
                "provider_record_identity": fact.provider_record_identity,
                "source_contract_version": fact.source_contract_version,
                "notional_semantics": fact.notional_semantics,
                "price_semantics": fact.price_semantics,
                "completeness_assumption": fact.completeness_assumption,
            },
        )
        bind.exec_driver_sql(
            """
            UPDATE public.news_items
               SET market_parse_status = 'parsed', market_parse_error = NULL
             WHERE item_id = %(item_id)s
            """,
            {"item_id": row.item_id},
        )


def downgrade() -> None:
    """Refused. The facts are the only structured record of those reports, and the reason is spent."""

    raise RuntimeError("news_smart_money_reparse_downgrade_unsupported")
