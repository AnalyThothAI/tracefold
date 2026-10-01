SELECT o.observation_id AS item_id, o.kind AS market_kind, o.source_strategy_id, o.parse_status, o.parse_error,
       o.ingest_mode, o.historical, o.title, o.event_at_ms, o.received_at_ms, o.available_at_ms, o.provider,
       o.source_venue, COALESCE(o.raw_instrument, e.token) AS raw_instrument, COALESCE(o.symbol, e.token_symbol) AS
           symbol,
       o.measurement_definition, o.measurement_window_ms,
       CASE WHEN o.oi_event_id IS NOT NULL AND o.source_contract_version IS NOT NULL AND o.measurement_window_ms >
           0 THEN 'proven'
            WHEN o.oi_event_id IS NOT NULL THEN 'unproven' END AS measurement_contract_status,
       o.direction, o.oi_change_bps, o.oi_value_usd, o.whale_long_profit_bps, o.whale_oi_ratio_bps,
       o.liquidated_position_side, o.forced_order_side, o.notional_usd::text AS notional_usd, o.price::text AS price,
       o.trader_label, o.account_address, o.action, o.position_side, o.pnl_usd::text AS pnl_usd,
       e.chain_id AS wallet_chain_id, e.token AS wallet_token, COALESCE(e.send_snapshot, e.initial_snapshot) AS
           wallet_snapshot,
       e.notification_eligible AS wallet_notify_eligible, e.notification_reason AS wallet_notification_reason,
       e.trigger_max_age_s AS wallet_trigger_max_age_s,
       o.notify_state, o.notify_group_key, o.notification_id AS delivery_key, d.state AS delivery_state,
       d.error AS delivery_error, d.trigger_item_id AS delivery_trigger_item_id, t.pending_reason AS track_reason,
       COALESCE(o.notification_id IS NULL AND o.received_at_ms < t.round_started_at_ms, false) AS round_closed,
       CASE WHEN o.oi_event_id IS NOT NULL THEN 'oi|' || o.provider || '|' || COALESCE(o.source_venue, '') || '|'
           || o.raw_instrument || '|' || o.measurement_definition
            WHEN o.liquidated_position_side IS NOT NULL THEN 'liquidation|' || o.provider || '|' ||
                COALESCE(o.source_venue, '') || '|' || o.raw_instrument || '|' || o.liquidated_position_side
            WHEN o.trader_label IS NOT NULL THEN 'smart_money|' || o.provider || '|' || o.source_strategy_id || '|'
                || o.trader_label || '|' || COALESCE(o.account_address, '') || '|' || COALESCE(o.source_venue, '')
                || '|' || o.raw_instrument || '|' || o.action || '|' || o.position_side
            WHEN e.item_id IS NOT NULL THEN 'wallet|net_buy|' || e.chain_id::text || '|' || e.token || '|' || e.item_id
            ELSE 'raw|' || o.kind || '|' || o.observation_id END AS group_key
  FROM news_market_observations o
  LEFT JOIN news_market_wallet_events e ON e.item_id = o.observation_id
  LEFT JOIN news_market_deliveries d ON d.delivery_key = o.notification_id
  LEFT JOIN news_market_tracks t ON t.group_key = o.notify_group_key
