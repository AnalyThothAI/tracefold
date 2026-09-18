"""Rewrite stored net-buy snapshots to the one window they are now evaluated under (#649 PR-3).

Migration evidence:
- category: one data rewrite of three jsonb columns on one small table. No column, index, constraint
  or grant changes.
- why_database_must_change: the alert has one rule from this revision on -- thirty minutes, five
  roster addresses, $1000 net each -- and the snapshot contract that describes it has one window
  instead of `fast` and `slow`, plus the two facts the card now carries: the tape's earliest sighting
  of the token, and each member's participation count. The stored snapshots are read back by
  `NetBuySnapshot.model_validate` in the detector (every slide of a live episode), in the API and in
  the send-time re-evaluation, and that model forbids unknown fields. A row left in the two-window
  shape would raise on the next turn that touched it, so the rows move with the code rather than
  being tolerated by it.
- current_source_revision: 20260915_0381
- minimum_supported_source_revision: 20260915_0381
- lock_level_and_order: ROW EXCLUSIVE on news_market_wallet_events for one UPDATE. Nothing else is
  touched, and no other table is locked.
- statement_timeout: 30s
- lock_timeout: 5s
- estimated_rows: the episodes retained by `news.chain_tape.retention_days`. Production has held no
  more than low tens since #641, and the table is bounded by one row per token episode.
- estimated_bytes: the rewritten rows are smaller than the originals: one window is stored instead of
  two, and the two added facts are one integer and one integer per member.
- rewrite_or_index_build: a single UPDATE touching every row of one small table; no index is built.
- preflight_and_maintenance_boundary: this is a coordinated cut, not an online one. The running image
  reads `fast`/`slow` and the new image reads `window`, so Workers and Serve are stopped for the
  deploy exactly as the repository's ordinary migration procedure requires. The operator must also
  delete `news.chain_tape.rules.net_buy_fast_n` from the deployment config first: the loader forbids
  unknown keys and the new image refuses to start while that line is there.
- archive_current_compatibility: nothing is archived. The 5-minute window carried no fact the
  30-minute window does not also carry over a longer span: its members are a subset of the surviving
  window's members, with the same arithmetic over a shorter slice, and the surviving window keeps
  every member row, every exclusion reason and every exact decimal.
- role_and_grant_impact: unchanged single application login.
- failure_state: transaction rollback leaves every snapshot exactly as it was.
- roll_forward_or_verified_backup_restore: downgrade restores the two-window shape, and it is lossy
  by construction -- the 5-minute slice it cannot recompute is written as an empty window, which is
  "no addresses recorded here" rather than a fabricated positive. A deployment that needs the old
  window's contents back restores a backup instead.
- production_postgres_image:
  postgres:18-bookworm@sha256:1961f96e6029a02c3812d7cb329a3b03a3ac2bb067058dec17b0f5596aca9296
"""

from alembic import op

revision = "20260918_0382"
down_revision = "20260915_0381"
branch_labels = None
depends_on = None

_FORWARD = """
CREATE FUNCTION pg_temp.wallet_one_window(snapshot jsonb) RETURNS jsonb LANGUAGE sql AS $$
    SELECT CASE WHEN snapshot IS NULL OR NOT (snapshot ? 'slow') THEN snapshot ELSE
        (snapshot - 'fast' - 'slow')
        || jsonb_build_object('token_first_seen_at_ms', NULL)
        || jsonb_build_object(
               'window',
               ((snapshot -> 'slow') - 'window')
               || jsonb_build_object(
                      'members',
                      COALESCE(
                          (
                              SELECT jsonb_agg(member || jsonb_build_object('recent_episodes', NULL)
                                               ORDER BY ordinality)
                                FROM jsonb_array_elements(snapshot -> 'slow' -> 'members')
                                     WITH ORDINALITY AS entry(member, ordinality)
                          ),
                          '[]'::jsonb
                      )
                  )
           )
    END
$$
"""

_BACKWARD = """
CREATE FUNCTION pg_temp.wallet_two_windows(snapshot jsonb) RETURNS jsonb LANGUAGE sql AS $$
    SELECT CASE WHEN snapshot IS NULL OR NOT (snapshot ? 'window') THEN snapshot ELSE
        (snapshot - 'window' - 'token_first_seen_at_ms')
        || jsonb_build_object(
               'slow',
               ((snapshot -> 'window') || jsonb_build_object('window', '30m'))
               || jsonb_build_object(
                      'members',
                      COALESCE(
                          (
                              SELECT jsonb_agg((member - 'recent_episodes') ORDER BY ordinality)
                                FROM jsonb_array_elements(snapshot -> 'window' -> 'members')
                                     WITH ORDINALITY AS entry(member, ordinality)
                          ),
                          '[]'::jsonb
                      )
                  )
           )
        || jsonb_build_object(
               'fast',
               jsonb_build_object(
                   'window', '5m',
                   'from_ms', (snapshot -> 'window' ->> 'to_ms')::bigint - 300000,
                   'to_ms', (snapshot -> 'window' ->> 'to_ms')::bigint,
                   'required_n', 3,
                   'qualified_n', 0,
                   'buy_usd', '0',
                   'sell_usd', '0',
                   'net_usd', '0',
                   'matched', false,
                   'members', '[]'::jsonb
               )
           )
    END
$$
"""

_UPDATE = """
    UPDATE news_market_wallet_events
       SET initial_snapshot = pg_temp.{fn}(initial_snapshot),
           latest_snapshot = pg_temp.{fn}(latest_snapshot),
           send_snapshot = pg_temp.{fn}(send_snapshot)
"""


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '30s'")
    op.execute(_FORWARD)
    op.execute(_UPDATE.format(fn="wallet_one_window"))
    op.execute("DROP FUNCTION pg_temp.wallet_one_window(jsonb)")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '30s'")
    op.execute(_BACKWARD)
    op.execute(_UPDATE.format(fn="wallet_two_windows"))
    op.execute("DROP FUNCTION pg_temp.wallet_two_windows(jsonb)")
