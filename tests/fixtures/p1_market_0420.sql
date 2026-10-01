-- Frozen market projection at P0 (55fad37ad), for populated P1 migration equivalence.

    SELECT i.item_id,
           i.market_kind,
           i.market_source_strategy_id AS source_strategy_id,
           i.market_parse_status AS parse_status,
           i.market_parse_error AS parse_error,
           i.first_ingest_mode AS ingest_mode,
           COALESCE(o.historical, false) AS historical,
           i.title,
           i.published_at_ms AS event_at_ms,
           i.observed_at_ms AS received_at_ms,
           COALESCE(o.available_at_ms, l.available_at_ms, w.available_at_ms) AS available_at_ms,
           -- Per row, never an implied constant. #553 stored the provider on every market fact
           -- precisely so a second one could not merge into the first's groups; `wallet` is that
           -- second provider, and it is the chain rather than OpenNews (#572 §5.2).
           COALESCE(o.provider, l.provider, w.provider,
                    CASE WHEN e.item_id IS NOT NULL THEN 'robinhood_chain' END) AS provider,
           COALESCE(o.source_venue, l.source_venue, w.source_venue) AS source_venue,
           COALESCE(o.raw_instrument, l.raw_instrument, w.raw_instrument, e.token) AS raw_instrument,
           COALESCE(o.symbol, l.symbol, w.symbol, e.token_symbol) AS symbol,
           o.measurement_definition,
           o.measurement_window_ms,
           CASE WHEN o.source_contract_version IS NOT NULL AND o.measurement_window_ms > 0
                THEN 'proven' WHEN o.source_item_id IS NOT NULL THEN 'unproven' END AS measurement_contract_status,
           o.direction,
           o.oi_change_bps,
           o.oi_value_usd,
           o.whale_long_profit_bps,
           o.whale_oi_ratio_bps,
           l.liquidated_position_side,
           l.forced_order_side,
           COALESCE(l.notional_usd, w.reported_notional_usd)::text AS notional_usd,
           COALESCE(l.price, w.price)::text AS price,
           w.trader_label,
           w.account_address,
           w.action,
           w.position_side,
           w.pnl_usd::text AS pnl_usd,
           e.chain_id AS wallet_chain_id,
           e.token AS wallet_token,
           COALESCE(e.send_snapshot, e.initial_snapshot) AS wallet_snapshot,
           e.notification_eligible AS wallet_notify_eligible,
           e.notification_reason AS wallet_notification_reason,
           e.trigger_max_age_s AS wallet_trigger_max_age_s,
           i.market_notify_state AS notify_state,
           i.market_notify_group_key AS notify_group_key,
           i.market_notify_delivery_key AS delivery_key,
           d.state AS delivery_state,
           d.error AS delivery_error,
           d.trigger_item_id AS delivery_trigger_item_id,
           t.pending_reason AS track_reason,
           -- No card claimed this observation and its group has moved on to a later round: nothing
           -- is holding it and nothing will cover it. The comparison lives here because the round
           -- start is the track's, and the track is already joined (#562 PR-F).
           COALESCE(i.market_notify_delivery_key IS NULL AND i.observed_at_ms < t.round_started_at_ms, false)
             AS round_closed,
           CASE
             WHEN o.source_item_id IS NOT NULL THEN
               'oi|' || o.provider || '|' || COALESCE(o.source_venue, '') || '|'
                     || o.raw_instrument || '|' || o.measurement_definition
             WHEN l.item_id IS NOT NULL THEN
               'liquidation|' || l.provider || '|' || COALESCE(l.source_venue, '') || '|'
                     || l.raw_instrument || '|' || l.liquidated_position_side
             WHEN w.item_id IS NOT NULL THEN
               'smart_money|' || w.provider || '|' || w.source_strategy_id || '|' || w.trader_label
                     || '|' || COALESCE(w.account_address, '') || '|' || COALESCE(w.source_venue, '')
                     || '|' || w.raw_instrument || '|' || w.action || '|' || w.position_side
             WHEN e.item_id IS NOT NULL THEN
               'wallet|net_buy|' || e.chain_id::text || '|' || e.token || '|' || e.item_id
             ELSE 'raw|' || i.market_kind || '|' || i.item_id
           END AS group_key
      FROM news_items i
      LEFT JOIN news_oi_signals o
             ON o.source_item_id = i.item_id
            AND o.metric_version = 'oi_signal_v1'
      LEFT JOIN news_market_liquidations l ON l.item_id = i.item_id
      LEFT JOIN news_market_smart_money w ON w.item_id = i.item_id
      LEFT JOIN news_market_wallet_events e ON e.item_id = i.item_id
      LEFT JOIN news_market_deliveries d ON d.delivery_key = i.market_notify_delivery_key
      LEFT JOIN news_market_tracks t ON t.group_key = i.market_notify_group_key
