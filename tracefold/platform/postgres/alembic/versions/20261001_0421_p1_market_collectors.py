"""#764 P1: market observations, membership intervals and collector documents.

Migration evidence:

- category: forward-only structural consolidation with projection-preserving backfill.
- why_database_must_change: separate market observations from editorial Items, replace repeated
  roster snapshots with membership intervals, and consolidate four typed collector identities.
- current_source_revision: 20261001_0420
- minimum_supported_source_revision: 20261001_0420
- lock_level_and_order: ACCESS EXCLUSIVE on altered/dropped tables; Serve/Workers/Analysis stopped.
- statement_timeout: 1800s
- lock_timeout: 5s
- estimated_rows: market observations, compacted roster, and 1,408 incident rows; data-dependent.
- estimated_bytes: size affected tables from the verified backup before deployment.
- rewrite_or_index_build: observation/interval/collector backfill and five observation indexes.
- preflight_and_maintenance_boundary: stop Serve/Workers/Analysis; export all ten dropped tables
  plus news_items/news_market_wallet_events, record sha256 and verify archive readability.
- archive_current_compatibility: preserve observation IDs, group keys, version IDs, OI outbox identity,
  notification backlog, scanned cursor and active/recovery-pending incidents.
- role_and_grant_impact: none; migration runs as the single tracefold owner.
- failure_state: one transactional revision; invalid source shapes or projection differences roll back.
- roll_forward_or_verified_backup_restore: verified backup restoration and matching old image.
- production_postgres_image:
  postgres:18-bookworm@sha256:1961f96e6029a02c3812d7cb329a3b03a3ac2bb067058dec17b0f5596aca9296

Revision ID: 20261001_0421
Revises: 20261001_0420
"""

from alembic import op

revision = "20261001_0421"
down_revision = "20261001_0420"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(r"""
-- #764 P1: market observations, wallets, collectors (baseline main 2f5c6cd45, DB head 20261001_0419 + P0).
-- Alembic upgrade() body; one transaction (BEGIN/COMMIT = validation harness).
-- Writers stopped (Serve/Workers/Analysis). Operator exported (pg_dump -t) every table dropped below first.
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '1800s';

DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM pg_stat_activity WHERE pid <> pg_backend_pid()
             AND datname=current_database() AND (application_name='tracefold_analysis'
               OR application_name LIKE 'tracefold_workers%' OR application_name LIKE 'tracefold_serve%')) THEN
    RAISE EXCEPTION 'p1_news_writers_connected: stop Serve, Workers and Analysis before upgrade';
  END IF;
END $$;

-- ===================== preconditions =====================
DO $$
DECLARE n bigint;
BEGIN
  SELECT count(*) INTO n FROM news_event_members m JOIN news_items i ON i.item_id = m.item_id WHERE i.market_kind IS
    NOT NULL;
  IF n > 0 THEN RAISE EXCEPTION 'p1_market_item_is_event_member: %', n; END IF;
  SELECT count(*) INTO n FROM news_events e JOIN news_items i ON i.item_id = e.leader_item_id WHERE i.market_kind IS
    NOT NULL;
  IF n > 0 THEN RAISE EXCEPTION 'p1_market_item_leads_event: %', n; END IF;
  SELECT count(*) INTO n FROM news_item_revisions r JOIN news_items i USING (item_id) WHERE i.market_kind IS NOT
    NULL;
  IF n > 0 THEN RAISE EXCEPTION 'p1_market_item_has_revisions: %', n; END IF;
  SELECT count(*) INTO n FROM news_items
   WHERE market_kind IS NOT NULL AND (evidence_text IS NOT NULL OR evidence_text_sha256 IS NOT NULL
         OR evidence_observed_at_ms IS NOT NULL OR provider_params_sha256 IS NOT NULL OR
    provider_params_available_at_ms IS NOT NULL);
  IF n > 0 THEN RAISE EXCEPTION 'p1_market_item_carries_editorial_evidence: %', n; END IF;
  SELECT count(*) INTO n FROM news_oi_signals o LEFT JOIN news_items i ON i.item_id = o.source_item_id
   WHERE i.market_kind IS DISTINCT FROM 'oi';
  IF n > 0 THEN RAISE EXCEPTION 'p1_oi_fact_without_oi_item: %', n; END IF;
  SELECT count(*) INTO n FROM news_market_liquidations l LEFT JOIN news_items i USING (item_id)
   WHERE i.market_kind IS DISTINCT FROM 'liquidation' OR l.ingest_mode <> i.first_ingest_mode;
  IF n > 0 THEN RAISE EXCEPTION 'p1_liquidation_fact_item_mismatch: %', n; END IF;
  SELECT count(*) INTO n FROM news_market_smart_money w LEFT JOIN news_items i USING (item_id)
   WHERE i.market_kind IS DISTINCT FROM 'smart_money' OR w.ingest_mode <> i.first_ingest_mode;
  IF n > 0 THEN RAISE EXCEPTION 'p1_smart_money_fact_item_mismatch: %', n; END IF;
  SELECT count(*) INTO n FROM news_market_wallet_events e LEFT JOIN news_items i USING (item_id)
   WHERE i.market_kind IS DISTINCT FROM 'wallet';
  IF n > 0 THEN RAISE EXCEPTION 'p1_wallet_episode_item_mismatch: %', n; END IF;
  -- dropped per-row contract columns are constants of parser_version (liquidations.py:164-176,
-- smart_money.py:92-96)
  SELECT count(*) INTO n FROM (
    SELECT parser_version FROM news_market_liquidations GROUP BY parser_version
    HAVING count(DISTINCT ROW(position_side_semantics, quantity_semantics, notional_semantics, price_semantics,
                              completeness_assumption, throttle_assumption, source_contract_complete)) > 1) x;
  IF n > 0 THEN RAISE EXCEPTION 'p1_liquidation_contract_not_constant_per_parser: %', n; END IF;
  SELECT count(*) INTO n FROM news_market_liquidations l JOIN news_items i USING (item_id)
   WHERE l.quantity IS NOT NULL OR l.provider_record_identity <> i.source_item_key
      OR l.symbol_contract_identity <> 'unresolved:' || COALESCE(l.source_venue, 'unknown') || ':' || l.symbol;
  IF n > 0 THEN RAISE EXCEPTION 'p1_liquidation_derived_columns_differ: %', n; END IF;
  SELECT count(*) INTO n FROM (
    SELECT parser_version FROM news_market_smart_money GROUP BY parser_version
    HAVING count(DISTINCT ROW(notional_semantics, price_semantics, completeness_assumption)) > 1) x;
  IF n > 0 THEN RAISE EXCEPTION 'p1_smart_money_contract_not_constant_per_parser: %', n; END IF;
  SELECT count(*) INTO n FROM news_market_smart_money w JOIN news_items i USING (item_id)
   WHERE w.provider_record_identity <> i.source_item_key;
  IF n > 0 THEN RAISE EXCEPTION 'p1_smart_money_record_identity_differs: %', n; END IF;
  -- fact-row clocks are copies of the Item's (admission.py:476-558 writes both from one _PreparedMarket); report
-- drift
  RAISE NOTICE 'p1 fact clocks differing from their Item (dropped, exported): oi=% liquidation=% smart_money=%',
    (SELECT count(*) FROM news_oi_signals o JOIN news_items i ON i.item_id = o.source_item_id
      WHERE (o.observed_at_ms, o.received_at_ms) IS DISTINCT FROM (i.published_at_ms, i.observed_at_ms)),
    (SELECT count(*) FROM news_market_liquidations l JOIN news_items i USING (item_id)
      WHERE (l.event_at_ms, l.received_at_ms) IS DISTINCT FROM (i.published_at_ms, i.observed_at_ms)),
    (SELECT count(*) FROM news_market_smart_money w JOIN news_items i USING (item_id)
      WHERE (w.event_at_ms, w.received_at_ms) IS DISTINCT FROM (i.published_at_ms, i.observed_at_ms));
  -- OI rows of a non-current metric version are invisible today (market.py:207-209, quote_storage.py:205);
-- exported, not migrated
  RAISE NOTICE 'p1 OI rows not migrated (metric_version <> oi_signal_v1): %',
    (SELECT count(*) FROM news_oi_signals WHERE metric_version <> 'oi_signal_v1');
END $$;

-- ===================== news_market_observations =====================
CREATE TABLE news_market_observations (
  observation_id text PRIMARY KEY,
  kind text NOT NULL CHECK (kind IN ('oi','liquidation','smart_money','unknown_market','wallet')),
  source_id text NOT NULL,
  source_item_key text NOT NULL,
  source_strategy_id text NOT NULL,
  provider_metadata jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(provider_metadata) = 'object'),
  provider_params jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(provider_params) = 'object'),
  title text NOT NULL,
  raw_first_line text NOT NULL DEFAULT '',
  description text NOT NULL DEFAULT '',
  ingest_mode text NOT NULL CHECK (ingest_mode IN ('live','recovery')),
  parse_status text NOT NULL CHECK (parse_status IN ('parsed','raw')),
  parse_error text,
  event_at_ms bigint NOT NULL,
  received_at_ms bigint NOT NULL,
  available_at_ms bigint,
  notify_state text NOT NULL CHECK (notify_state IN ('pending','historical','processed')),
  notify_group_key text,
  notification_id text REFERENCES news_market_deliveries(delivery_key) ON DELETE SET NULL,
  provider text,
  source_venue text,
  raw_instrument text,
  symbol text,
  parser_version text,
  source_contract_version text,
  historical boolean NOT NULL DEFAULT false,
  oi_event_id text,
  measurement_definition text,
  measurement_window_ms bigint CHECK (measurement_window_ms > 0),
  direction text CHECK (direction IN ('rise','fall')),
  oi_change_bps bigint,
  oi_value_usd bigint,
  whale_long_profit_bps bigint,
  whale_oi_ratio_bps bigint,
  liquidated_position_side text CHECK (liquidated_position_side IN ('long','short')),
  forced_order_side text CHECK (forced_order_side IN ('buy','sell')),
  notional_usd numeric CHECK (notional_usd > 0),
  price numeric CHECK (price > 0),
  trader_label text,
  account_address text,
  action text CHECK (action IN ('open','close')),
  position_side text CHECK (position_side IN ('long','short')),
  pnl_usd numeric,
  created_at_ms bigint NOT NULL,
  updated_at_ms bigint NOT NULL,
  CONSTRAINT news_market_observations_parse_check CHECK (((parse_status = 'parsed') = (parse_error IS NULL)) IS
    TRUE),
  CONSTRAINT news_market_observations_notify_check CHECK ((
    (notify_state = 'processed' OR (notify_group_key IS NULL AND notification_id IS NULL))
    AND (notification_id IS NULL OR notify_group_key IS NOT NULL)) IS TRUE)
);

-- the old projection, frozen before anything moves (market.py:135-215)
CREATE TEMP TABLE p1_old_projection ON COMMIT DROP AS
SELECT i.item_id, i.market_kind, i.market_source_strategy_id AS source_strategy_id, i.market_parse_status AS
    parse_status,
       i.market_parse_error AS parse_error, i.first_ingest_mode AS ingest_mode, COALESCE(o.historical, false) AS
    historical,
       i.title, i.published_at_ms AS event_at_ms, i.observed_at_ms AS received_at_ms,
       COALESCE(o.available_at_ms, l.available_at_ms, w.available_at_ms) AS available_at_ms,
       COALESCE(o.provider, l.provider, w.provider, CASE WHEN e.item_id IS NOT NULL THEN 'robinhood_chain' END) AS
    provider,
       COALESCE(o.source_venue, l.source_venue, w.source_venue) AS source_venue,
       COALESCE(o.raw_instrument, l.raw_instrument, w.raw_instrument, e.token) AS raw_instrument,
       COALESCE(o.symbol, l.symbol, w.symbol, e.token_symbol) AS symbol,
       o.measurement_definition, o.measurement_window_ms,
       CASE WHEN o.source_contract_version IS NOT NULL AND o.measurement_window_ms > 0 THEN 'proven'
            WHEN o.source_item_id IS NOT NULL THEN 'unproven' END AS measurement_contract_status,
       o.direction, o.oi_change_bps, o.oi_value_usd, o.whale_long_profit_bps, o.whale_oi_ratio_bps,
       l.liquidated_position_side, l.forced_order_side,
       COALESCE(l.notional_usd, w.reported_notional_usd)::text AS notional_usd, COALESCE(l.price, w.price)::text AS
    price,
       w.trader_label, w.account_address, w.action, w.position_side, w.pnl_usd::text AS pnl_usd,
       e.chain_id AS wallet_chain_id, e.token AS wallet_token, COALESCE(e.send_snapshot, e.initial_snapshot) AS
    wallet_snapshot,
       e.notification_eligible AS wallet_notify_eligible, e.notification_reason AS wallet_notification_reason,
       e.trigger_max_age_s AS wallet_trigger_max_age_s,
       i.market_notify_state AS notify_state, i.market_notify_group_key AS notify_group_key,
       i.market_notify_delivery_key AS delivery_key, d.state AS delivery_state, d.error AS delivery_error,
       d.trigger_item_id AS delivery_trigger_item_id, t.pending_reason AS track_reason,
       COALESCE(i.market_notify_delivery_key IS NULL AND i.observed_at_ms < t.round_started_at_ms, false) AS
    round_closed,
       CASE WHEN o.source_item_id IS NOT NULL THEN 'oi|' || o.provider || '|' || COALESCE(o.source_venue, '') || '|'
    || o.raw_instrument || '|' || o.measurement_definition
            WHEN l.item_id IS NOT NULL THEN 'liquidation|' || l.provider || '|' || COALESCE(l.source_venue, '') ||
    '|' || l.raw_instrument || '|' || l.liquidated_position_side
            WHEN w.item_id IS NOT NULL THEN 'smart_money|' || w.provider || '|' || w.source_strategy_id || '|' ||
    w.trader_label || '|' || COALESCE(w.account_address, '') || '|' || COALESCE(w.source_venue, '') || '|' ||
    w.raw_instrument || '|' || w.action || '|' || w.position_side
            WHEN e.item_id IS NOT NULL THEN 'wallet|net_buy|' || e.chain_id::text || '|' || e.token || '|' ||
    e.item_id
            ELSE 'raw|' || i.market_kind || '|' || i.item_id END AS group_key,
       i.provider_params, i.description, i.raw_first_line
  FROM news_items i
  LEFT JOIN news_oi_signals o ON o.source_item_id = i.item_id AND o.metric_version = 'oi_signal_v1'
  LEFT JOIN news_market_liquidations l ON l.item_id = i.item_id
  LEFT JOIN news_market_smart_money w ON w.item_id = i.item_id
  LEFT JOIN news_market_wallet_events e ON e.item_id = i.item_id
  LEFT JOIN news_market_deliveries d ON d.delivery_key = i.market_notify_delivery_key
  LEFT JOIN news_market_tracks t ON t.group_key = i.market_notify_group_key
 WHERE i.market_kind IS NOT NULL;

INSERT INTO news_market_observations (
  observation_id, kind, source_id, source_item_key, source_strategy_id, provider_metadata, provider_params, title,
  raw_first_line, description, ingest_mode, parse_status, parse_error, event_at_ms, received_at_ms, available_at_ms,
  notify_state, notify_group_key, notification_id, provider, source_venue, raw_instrument, symbol, parser_version,
  source_contract_version, historical, oi_event_id, measurement_definition, measurement_window_ms, direction,
  oi_change_bps, oi_value_usd, whale_long_profit_bps, whale_oi_ratio_bps, liquidated_position_side,
    forced_order_side,
  notional_usd, price, trader_label, account_address, action, position_side, pnl_usd, created_at_ms, updated_at_ms)
SELECT i.item_id, i.market_kind, i.source_id, i.source_item_key, i.market_source_strategy_id, i.provider_metadata,
       i.provider_params, i.title, i.raw_first_line, i.description, i.first_ingest_mode, i.market_parse_status,
       i.market_parse_error, i.published_at_ms, i.observed_at_ms,
       COALESCE(o.available_at_ms, l.available_at_ms, w.available_at_ms),
       i.market_notify_state, i.market_notify_group_key, i.market_notify_delivery_key,
       COALESCE(o.provider, l.provider, w.provider, CASE WHEN e.item_id IS NOT NULL THEN 'robinhood_chain' END),
       COALESCE(o.source_venue, l.source_venue, w.source_venue),
       COALESCE(o.raw_instrument, l.raw_instrument, w.raw_instrument),
       COALESCE(o.symbol, l.symbol, w.symbol),
       COALESCE(o.metric_version, l.parser_version, w.parser_version),
       COALESCE(o.source_contract_version, l.source_contract_version, w.source_contract_version),
       COALESCE(o.historical, false), o.event_id, o.measurement_definition, o.measurement_window_ms, o.direction,
       o.oi_change_bps, o.oi_value_usd, o.whale_long_profit_bps, o.whale_oi_ratio_bps,
       l.liquidated_position_side, l.forced_order_side,
       COALESCE(l.notional_usd, w.reported_notional_usd), COALESCE(l.price, w.price),
       w.trader_label, w.account_address, w.action, w.position_side, w.pnl_usd,
       i.created_at_ms, i.updated_at_ms
  FROM news_items i
  LEFT JOIN news_oi_signals o ON o.source_item_id = i.item_id AND o.metric_version = 'oi_signal_v1'
  LEFT JOIN news_market_liquidations l ON l.item_id = i.item_id
  LEFT JOIN news_market_smart_money w ON w.item_id = i.item_id
  LEFT JOIN news_market_wallet_events e ON e.item_id = i.item_id
 WHERE i.market_kind IS NOT NULL;

-- retarget the two child FKs BEFORE market items leave news_items (their old FKs cascade)
ALTER TABLE news_market_wallet_events DROP CONSTRAINT news_market_wallet_events_item_id_fkey;
ALTER TABLE news_market_wallet_events ADD CONSTRAINT news_market_wallet_events_observation_fkey
  FOREIGN KEY (item_id) REFERENCES news_market_observations(observation_id) ON DELETE CASCADE;
ALTER TABLE news_market_deliveries DROP CONSTRAINT news_market_deliveries_trigger_fk;
ALTER TABLE news_market_deliveries ADD CONSTRAINT news_market_deliveries_trigger_fk
  FOREIGN KEY (trigger_item_id) REFERENCES news_market_observations(observation_id) ON DELETE CASCADE;

-- the new projection (target market.py _OBSERVATIONS_SQL) must equal the old one row for row
CREATE TEMP TABLE p1_new_projection ON COMMIT DROP AS
SELECT o.observation_id AS item_id, o.kind AS market_kind, o.source_strategy_id, o.parse_status, o.parse_error,
       o.ingest_mode, o.historical, o.title, o.event_at_ms, o.received_at_ms, o.available_at_ms, o.provider,
       o.source_venue, COALESCE(o.raw_instrument, e.token) AS raw_instrument, COALESCE(o.symbol, e.token_symbol) AS
    symbol,
       o.measurement_definition, o.measurement_window_ms,
       CASE WHEN o.oi_event_id IS NOT NULL AND o.source_contract_version IS NOT NULL AND o.measurement_window_ms > 0
    THEN 'proven'
            WHEN o.oi_event_id IS NOT NULL THEN 'unproven' END AS measurement_contract_status,
       o.direction, o.oi_change_bps, o.oi_value_usd, o.whale_long_profit_bps, o.whale_oi_ratio_bps,
       o.liquidated_position_side, o.forced_order_side, o.notional_usd::text AS notional_usd, o.price::text AS
    price,
       o.trader_label, o.account_address, o.action, o.position_side, o.pnl_usd::text AS pnl_usd,
       e.chain_id AS wallet_chain_id, e.token AS wallet_token, COALESCE(e.send_snapshot, e.initial_snapshot) AS
    wallet_snapshot,
       e.notification_eligible AS wallet_notify_eligible, e.notification_reason AS wallet_notification_reason,
       e.trigger_max_age_s AS wallet_trigger_max_age_s,
       o.notify_state, o.notify_group_key, o.notification_id AS delivery_key, d.state AS delivery_state,
       d.error AS delivery_error, d.trigger_item_id AS delivery_trigger_item_id, t.pending_reason AS track_reason,
       COALESCE(o.notification_id IS NULL AND o.received_at_ms < t.round_started_at_ms, false) AS round_closed,
       CASE WHEN o.oi_event_id IS NOT NULL THEN 'oi|' || o.provider || '|' || COALESCE(o.source_venue, '') || '|' ||
    o.raw_instrument || '|' || o.measurement_definition
            WHEN o.liquidated_position_side IS NOT NULL THEN 'liquidation|' || o.provider || '|' ||
    COALESCE(o.source_venue, '') || '|' || o.raw_instrument || '|' || o.liquidated_position_side
            WHEN o.trader_label IS NOT NULL THEN 'smart_money|' || o.provider || '|' || o.source_strategy_id || '|'
    || o.trader_label || '|' || COALESCE(o.account_address, '') || '|' || COALESCE(o.source_venue, '') || '|' ||
    o.raw_instrument || '|' || o.action || '|' || o.position_side
            WHEN e.item_id IS NOT NULL THEN 'wallet|net_buy|' || e.chain_id::text || '|' || e.token || '|' ||
    e.item_id
            ELSE 'raw|' || o.kind || '|' || o.observation_id END AS group_key,
       o.provider_params, o.description, o.raw_first_line
  FROM news_market_observations o
  LEFT JOIN news_market_wallet_events e ON e.item_id = o.observation_id
  LEFT JOIN news_market_deliveries d ON d.delivery_key = o.notification_id
  LEFT JOIN news_market_tracks t ON t.group_key = o.notify_group_key;

DO $$
DECLARE a bigint; b bigint;
BEGIN
  SELECT count(*) INTO a FROM (SELECT * FROM p1_old_projection EXCEPT ALL SELECT * FROM p1_new_projection) x;
  SELECT count(*) INTO b FROM (SELECT * FROM p1_new_projection EXCEPT ALL SELECT * FROM p1_old_projection) x;
  IF a <> 0 OR b <> 0 THEN RAISE EXCEPTION 'p1_market_projection_mismatch old_only=% new_only=%', a, b; END IF;
  IF (SELECT count(*) FROM news_market_observations WHERE oi_event_id IS NOT NULL)
     <> (SELECT count(*) FROM news_oi_signals WHERE metric_version = 'oi_signal_v1') THEN RAISE EXCEPTION
    'p1_oi_count'; END IF;
  IF (SELECT count(*) FROM news_market_observations WHERE liquidated_position_side IS NOT NULL)
     <> (SELECT count(*) FROM news_market_liquidations) THEN RAISE EXCEPTION 'p1_liquidation_count'; END IF;
  IF (SELECT count(*) FROM news_market_observations WHERE trader_label IS NOT NULL)
     <> (SELECT count(*) FROM news_market_smart_money) THEN RAISE EXCEPTION 'p1_smart_money_count'; END IF;
  RAISE NOTICE 'p1 observations=% (oi=%, liquidation=%, smart_money=%, wallet=%, unknown=%)',
    (SELECT count(*) FROM news_market_observations),
    (SELECT count(*) FROM news_market_observations WHERE kind = 'oi'),
    (SELECT count(*) FROM news_market_observations WHERE kind = 'liquidation'),
    (SELECT count(*) FROM news_market_observations WHERE kind = 'smart_money'),
    (SELECT count(*) FROM news_market_observations WHERE kind = 'wallet'),
    (SELECT count(*) FROM news_market_observations WHERE kind = 'unknown_market');
END $$;

DROP TABLE news_oi_signals;
DROP TABLE news_market_liquidations;
DROP TABLE news_market_smart_money;
DELETE FROM news_items WHERE market_kind IS NOT NULL;
ALTER TABLE news_items
  DROP COLUMN market_kind, DROP COLUMN market_source_strategy_id, DROP COLUMN market_parse_status,
  DROP COLUMN market_parse_error, DROP COLUMN market_notify_state, DROP COLUMN market_notify_group_key,
  DROP COLUMN market_notify_delivery_key;

CREATE INDEX news_market_observations_received ON news_market_observations (received_at_ms DESC, observation_id
    DESC);
CREATE INDEX news_market_observations_pending ON news_market_observations (received_at_ms, observation_id) WHERE
    notify_state = 'pending';
CREATE INDEX news_market_observations_notify_group ON news_market_observations (notify_group_key, received_at_ms)
    WHERE notify_group_key IS NOT NULL;
CREATE INDEX news_market_observations_notification ON news_market_observations (notification_id) WHERE
    notification_id IS NOT NULL;
CREATE INDEX news_market_observations_oi_event_time ON news_market_observations (event_at_ms) WHERE kind = 'oi';

-- ===================== optional modules switched off: reactions, wallet outcomes =====================
DO $$ BEGIN
  RAISE NOTICE 'p1 dropping news_event_reactions rows=% news_market_wallet_outcomes rows=%',
    (SELECT count(*) FROM news_event_reactions), (SELECT count(*) FROM news_market_wallet_outcomes);
END $$;
DROP TABLE news_market_wallet_outcomes;
DROP TABLE news_event_reactions;
ALTER TABLE news_market_wallet_events
  DROP COLUMN reference_price, DROP COLUMN reference_at_ms, DROP COLUMN reference_source, DROP COLUMN
    outcome_attempted_at_ms;

-- ===================== news_market_wallets (membership intervals over the P0-compacted versions)
-- =====================
CREATE TABLE news_market_wallets (
  wallet text NOT NULL CHECK (wallet ~ '^0x[0-9a-f]{40}$'),
  joined_version bigint NOT NULL CHECK (joined_version > 0),
  joined_at_ms bigint NOT NULL,
  left_version bigint,
  left_at_ms bigint,
  handle text NOT NULL DEFAULT '',
  provider text NOT NULL DEFAULT 'robinhoodtrenches' CHECK (provider = 'robinhoodtrenches'),
  monitoring_from_ms bigint,
  PRIMARY KEY (wallet, joined_version),
  CONSTRAINT news_market_wallets_interval_check CHECK ((
    ((left_version IS NULL) = (left_at_ms IS NULL)) AND (left_version IS NULL OR left_version > joined_version)) IS
    TRUE)
);

CREATE TEMP TABLE p1_versions ON COMMIT DROP AS
SELECT roster_version, min(known_at_ms) AS known_at_ms,
       row_number() OVER (ORDER BY roster_version) AS ordinal
  FROM news_market_wallet_roster GROUP BY roster_version;

INSERT INTO news_market_wallets (wallet, joined_version, joined_at_ms, left_version, left_at_ms, handle, provider,
    monitoring_from_ms)
WITH member AS (
  SELECT r.wallet, r.handle, r.provider, r.monitoring_from_ms, v.roster_version, v.ordinal,
         v.ordinal - row_number() OVER (PARTITION BY r.wallet ORDER BY v.ordinal) AS island
    FROM news_market_wallet_roster r JOIN p1_versions v USING (roster_version)
), interval AS (
  SELECT wallet, island, min(ordinal) AS first_ordinal, max(ordinal) AS last_ordinal FROM member GROUP BY wallet,
    island
)
SELECT i.wallet, jv.roster_version, jv.known_at_ms, lv.roster_version, lv.known_at_ms,
       last.handle, last.provider, last.monitoring_from_ms
  FROM interval i
  JOIN p1_versions jv ON jv.ordinal = i.first_ordinal
  LEFT JOIN p1_versions lv ON lv.ordinal = i.last_ordinal + 1
  JOIN member last ON last.wallet = i.wallet AND last.ordinal = i.last_ordinal;

DO $$
DECLARE n bigint;
BEGIN
  -- every (version, wallet) of the roster is reproduced by the intervals, and nothing else is
  SELECT count(*) INTO n FROM (
    SELECT v.roster_version, w.wallet FROM news_market_wallets w JOIN p1_versions v
        ON w.joined_version <= v.roster_version AND (w.left_version IS NULL OR w.left_version > v.roster_version)
    EXCEPT SELECT roster_version, wallet FROM news_market_wallet_roster) x;
  IF n > 0 THEN RAISE EXCEPTION 'p1_wallet_intervals_extra_members: %', n; END IF;
  SELECT count(*) INTO n FROM (
    SELECT roster_version, wallet FROM news_market_wallet_roster
    EXCEPT SELECT v.roster_version, w.wallet FROM news_market_wallets w JOIN p1_versions v
        ON w.joined_version <= v.roster_version AND (w.left_version IS NULL OR w.left_version > v.roster_version))
    x;
  IF n > 0 THEN RAISE EXCEPTION 'p1_wallet_intervals_missing_members: %', n; END IF;
  -- each version's known_at_ms is recoverable from the interval edges (chain_tape_members)
  SELECT count(*) INTO n FROM p1_versions v
   WHERE v.known_at_ms IS DISTINCT FROM (
     SELECT max(t) FROM (SELECT joined_at_ms AS t FROM news_market_wallets WHERE joined_version = v.roster_version
                         UNION ALL SELECT left_at_ms FROM news_market_wallets WHERE left_version = v.roster_version)
    e);
  IF n > 0 THEN RAISE EXCEPTION 'p1_wallet_version_time_not_recoverable: %', n; END IF;
  -- the current version is reproduced exactly (handle, monitoring) -- older versions keep the interval's latest
-- values
  SELECT count(*) INTO n FROM news_market_wallet_roster r
    LEFT JOIN news_market_wallets w ON w.wallet = r.wallet AND w.left_version IS NULL
   WHERE r.roster_version = (SELECT max(roster_version) FROM news_market_wallet_roster)
     AND (w.wallet IS NULL OR (w.handle, w.monitoring_from_ms) IS DISTINCT FROM (r.handle, r.monitoring_from_ms));
  IF n > 0 THEN RAISE EXCEPTION 'p1_wallet_current_roster_mismatch: %', n; END IF;
  RAISE NOTICE 'p1 wallet intervals=% from roster rows=%; historical rows whose monitoring differs from their
    interval=%',
    (SELECT count(*) FROM news_market_wallets), (SELECT count(*) FROM news_market_wallet_roster),
    (SELECT count(*) FROM news_market_wallet_roster r JOIN news_market_wallets w
         ON w.wallet = r.wallet AND w.joined_version <= r.roster_version AND (w.left_version IS NULL OR
    w.left_version > r.roster_version)
      WHERE r.monitoring_from_ms IS DISTINCT FROM w.monitoring_from_ms);
END $$;

CREATE UNIQUE INDEX news_market_wallets_current ON news_market_wallets (wallet) WHERE left_version IS NULL;
CREATE INDEX news_market_wallets_unmonitored ON news_market_wallets (wallet) WHERE monitoring_from_ms IS NULL;

-- ===================== news_collectors =====================
CREATE TABLE news_collectors (
  collector_id text PRIMARY KEY CHECK (collector_id IN
    ('opennews','chain_tape','wallet_roster','instrument_catalog')),
  state jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(state) = 'object'),
  incidents jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(incidents) = 'array'),
  updated_at_ms bigint NOT NULL
);

INSERT INTO news_collectors (collector_id, state, incidents, updated_at_ms)
SELECT 'opennews',
       jsonb_build_object('connected', s.connected, 'last_frame_at_ms', s.last_frame_at_ms,
                          'last_publish_at_ms', s.last_publish_at_ms, 'last_error_code', s.last_error_code,
                          'broker_snapshot', s.broker_snapshot,
                          'next_incident_id', GREATEST(
                              (SELECT COALESCE(max(incident_id), 0) + 1 FROM news_opennews_incidents),
                              (SELECT CASE WHEN is_called THEN last_value + 1 ELSE last_value END
                                 FROM news_opennews_incidents_incident_id_seq))),
       COALESCE((SELECT jsonb_agg(to_jsonb(k) - 'created_at_ms' ORDER BY k.incident_id)
                   FROM (SELECT * FROM news_opennews_incidents
                          WHERE closed_at_ms IS NULL OR recovery_status = 'pending'
                         UNION ALL
                         SELECT * FROM (SELECT * FROM news_opennews_incidents
                                         WHERE closed_at_ms IS NOT NULL AND recovery_status <> 'pending'
                                         ORDER BY incident_id DESC LIMIT 20) recent) k), '[]'::jsonb),
       s.updated_at_ms
  FROM news_ingest_state s WHERE s.singleton_key = 'opennews';

INSERT INTO news_collectors (collector_id, state, updated_at_ms)
SELECT 'chain_tape',
       to_jsonb(t) - ARRAY['state_id','updated_at_ms','roster_last_attempt_at_ms','roster_last_success_at_ms',
                           'roster_last_error','roster_next_attempt_at_ms','roster_consecutive_failures'],
       t.updated_at_ms
  FROM news_market_wallet_tape_state t WHERE t.state_id = 'chain_tape';

INSERT INTO news_collectors (collector_id, state, updated_at_ms)
SELECT 'wallet_roster',
       jsonb_build_object('last_attempt_at_ms', t.roster_last_attempt_at_ms,
                          'last_success_at_ms', COALESCE(t.roster_last_success_at_ms,
                              (SELECT max(taken_at_ms) FROM news_market_wallet_roster
                                WHERE roster_version = (SELECT max(roster_version) FROM
    news_market_wallet_roster))),
                          'last_error', t.roster_last_error, 'next_attempt_at_ms', t.roster_next_attempt_at_ms,
                          'consecutive_failures', t.roster_consecutive_failures),
       COALESCE(t.roster_last_attempt_at_ms, t.updated_at_ms)
  FROM news_market_wallet_tape_state t WHERE t.state_id = 'chain_tape';

INSERT INTO news_collectors (collector_id, state, updated_at_ms)
SELECT 'instrument_catalog', jsonb_build_object('venues', jsonb_object_agg(venue, last_snapshot_ms)),
    max(last_snapshot_ms)
  FROM news_market_instrument_snapshot_state HAVING count(*) > 0;

DO $$
DECLARE n bigint;
BEGIN
  SELECT count(*) INTO n FROM news_opennews_incidents k
   WHERE (k.closed_at_ms IS NULL OR k.recovery_status = 'pending')
     AND NOT EXISTS (SELECT 1 FROM news_collectors c, jsonb_array_elements(c.incidents) x
                      WHERE c.collector_id = 'opennews' AND (x ->> 'incident_id')::bigint = k.incident_id
                        AND x = to_jsonb(k) - 'created_at_ms');
  IF n > 0 THEN RAISE EXCEPTION 'p1_active_incident_not_carried: %', n; END IF;
  IF EXISTS (SELECT 1 FROM news_market_wallet_tape_state t, news_collectors c
              WHERE c.collector_id = 'chain_tape' AND t.state_id = 'chain_tape'
                AND (c.state ->> 'scanned_block')::bigint IS DISTINCT FROM t.scanned_block) THEN
    RAISE EXCEPTION 'p1_chain_tape_cursor_mismatch';
  END IF;
  IF (SELECT count(*) FROM news_ingest_state) <> (SELECT count(*) FROM news_collectors WHERE collector_id =
    'opennews') THEN
    RAISE EXCEPTION 'p1_opennews_collector_missing';
  END IF;
  RAISE NOTICE 'p1 incidents: % rows -> % carried (active + newest 20 settled); roster taken_at vs last_success
    differ=%',
    (SELECT count(*) FROM news_opennews_incidents),
    (SELECT jsonb_array_length(incidents) FROM news_collectors WHERE collector_id = 'opennews'),
    (SELECT count(*) FROM news_market_wallet_tape_state t
      WHERE t.roster_last_success_at_ms IS DISTINCT FROM (SELECT max(taken_at_ms) FROM news_market_wallet_roster
                                                          WHERE roster_version = (SELECT max(roster_version) FROM
    news_market_wallet_roster)));
END $$;

-- Empty source databases still have all four typed collector identities.
INSERT INTO news_collectors(collector_id,state,updated_at_ms) VALUES
    ('opennews','{"connected": false,"last_frame_at_ms": null,"last_publish_at_ms": null,"last_error_code":
    null,"broker_snapshot": {},"next_incident_id": 1}'::jsonb,0)
    ON CONFLICT DO NOTHING;
INSERT INTO news_collectors(collector_id,state,updated_at_ms) VALUES
    ('chain_tape','{"high_water_block": 0,"high_water_tx_index": -1,"roster_version": 0,"last_outcome":
    "","last_error": null,"last_success_at_ms": null,"ignored_inbound_total": 0,"unknown_total":
    0,"noise_through_block": 0,"noise_through_tx_index": -1,"detection_cutover_at_ms": 0,"coverage_from_ms":
    null,"scanned_at_ms": null,"scanned_block": null,"scanned_log": null,"gap_at_ms": null,"next_attempt_at_ms":
    0,"consecutive_failures": 0,"blocked_tx_hash": null,"enrichment_error": null}'::jsonb
    || jsonb_build_object('detection_cutover_at_ms',(EXTRACT(epoch FROM clock_timestamp())*1000)::bigint),0) ON
    CONFLICT DO NOTHING;
INSERT INTO news_collectors(collector_id,state,updated_at_ms) VALUES
    ('wallet_roster','{"last_attempt_at_ms": null,"last_success_at_ms": null,"last_error":
    null,"next_attempt_at_ms": 0,"consecutive_failures": 0}'::jsonb
    || jsonb_build_object('last_success_at_ms',(SELECT max(taken_at_ms) FROM news_market_wallet_roster WHERE
    roster_version=(SELECT max(roster_version) FROM news_market_wallet_roster))),0) ON CONFLICT DO NOTHING;
INSERT INTO news_collectors(collector_id,state,updated_at_ms) VALUES ('instrument_catalog','{"venues": {}}'::jsonb,0)
    ON CONFLICT DO NOTHING;

DROP TABLE news_market_wallet_roster;
DROP TABLE news_market_wallet_tape_state;
DROP TABLE news_market_instrument_snapshot_state;
DROP TABLE news_opennews_incidents;  -- owns news_opennews_incidents_incident_id_seq
DROP TABLE news_ingest_state;
""")


def downgrade() -> None:
    raise RuntimeError("p1_forward_only: restore verified backup and matching image")
