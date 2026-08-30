"""Upgrade evidence for #288's durable Event kind and Program route hard cut."""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest
from alembic import command
from psycopg.errors import CheckViolation, NotNullViolation

from tests.postgres_test_utils import connect_postgres_test, reset_postgres_schema
from tests.postgres_test_utils import test_postgres_dsn as postgres_test_dsn
from tracefold.app.repository_session import repositories_for_connection
from tracefold.news.events.facts import extract_fact_units
from tracefold.news.events.identity import dedupe_family
from tracefold.news.events.titles import extract_title
from tracefold.news.opennews import OPENNEWS_SOURCE_ID, parse_opennews_message
from tracefold.news.pipeline.admission import admit_item, item_identity
from tracefold.platform.postgres.migrations import alembic_config

pytestmark = [pytest.mark.integration, pytest.mark.migration, pytest.mark.usefixtures("postgres_migration_dsn")]

BEFORE_EVENT_KIND = "20260827_0314"
NOW = 1_900_000_000_000
TAXONOMY_V1_PROGRAM_SHA256 = "0cabb7c74daa023e30a6433d33425d9d73082c2bd91f9eb1bd1c2c43d6b30d24"
TAXONOMY_V1_ENVELOPE_SHA256 = "4775cab09894b693fe825afdaec2b27aa2b76b2f206d9412bc790aea4935d90d"
TAXONOMY_V1_CODEBOOK_SHA256 = "6f978685c1ffeb6615bfb5dc05eecb9004ebb6f7de8732602e2823d09a12daac"
TAXONOMY_V1_PROGRAM_VERSION = "news_semantic_program_v7"
TAXONOMY_V1_VERSION = "news_taxonomy_v1"
TAXONOMY_V1_REVIEW_RUBRIC_VERSION = "news_review_v5"


def _upgrade(revision: str) -> None:
    config = alembic_config()
    config.attributes["database_url"] = postgres_test_dsn()
    command.upgrade(config, revision)


def _fresh_schema_at(revision: str) -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.execute("GRANT ALL ON SCHEMA public TO public")
        conn.commit()
    finally:
        conn.close()
    _upgrade(revision)


def _seed_event(
    conn: Any,
    event_id: str,
    *,
    strategy: tuple[str, str, str, str] | None,
    score: float | None = None,
    admission: str = "suppressed_low_signal",
    ingest_mode: str = "live",
    item_id: str | None = None,
    source_item_key: str | None = None,
    title: str = "headline",
    fact_id: str | None = None,
    comparison_fingerprint: str | None = None,
    family: str = "general",
) -> None:
    resolved_item_id = item_id or f"i-{event_id}"
    resolved_source_key = source_item_key or f"k-{event_id}"
    resolved_fact_id = fact_id or f"fact:{event_id}"
    resolved_fingerprint = comparison_fingerprint or f"fp-{event_id}"
    metadata: dict[str, Any] = {}
    if strategy is not None:
        strategy_id, name, source_type, engine_type = strategy
        metadata["strategies"] = [
            {
                "id": strategy_id,
                "name": name,
                "source_type": source_type,
                "engine_type": engine_type,
            }
        ]
    if score is not None:
        metadata["score"] = score
    conn.execute(
        """
        INSERT INTO news_items (
          item_id, source_id, source_item_key, title, published_at_ms, observed_at_ms,
          provider_metadata, first_ingest_mode, created_at_ms, updated_at_ms
        ) VALUES (%s, 'opennews', %s, %s, %s, %s, %s::jsonb, %s, %s, %s)
        """,
        (
            resolved_item_id,
            resolved_source_key,
            title,
            NOW,
            NOW,
            json.dumps(metadata, ensure_ascii=False),
            ingest_mode,
            NOW,
            NOW,
        ),
    )
    conn.execute(
        """
        INSERT INTO news_events (
          event_id, leader_item_id, family, comparison_fingerprint, comparison_title, leader_title,
          focus_fact_id, opened_at_ms, last_member_at_ms, expires_at_ms, admission, ingest_mode,
          created_at_ms, updated_at_ms
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                  %s, %s, %s, %s)
        """,
        (
            event_id,
            resolved_item_id,
            family,
            resolved_fingerprint,
            title,
            title,
            resolved_fact_id,
            NOW,
            NOW,
            NOW + 1,
            admission,
            ingest_mode,
            NOW,
            NOW,
        ),
    )
    conn.execute(
        """
        INSERT INTO news_event_members(
          event_id, item_id, joined_at_ms, match_kind, jaccard_estimate, fact_id, fact_text
        ) VALUES (%s, %s, %s, 'leader', NULL, %s, %s)
        """,
        (event_id, resolved_item_id, NOW, resolved_fact_id, title),
    )


def test_0315_backfills_exact_source_contracts_and_records_the_factory_hard_cut() -> None:
    conn: Any | None = None
    try:
        _fresh_schema_at(BEFORE_EVENT_KIND)
        conn = connect_postgres_test(read_only=False)
        rows = {
            "oi": ("1019", "OI Event Monitor", "market", "market"),
            "legacy-recovery-oi": ("1019", "OI Event Monitor", "market", "market"),
            "listing": ("1353", "Listing and Delisting Announcements", "news", "listing"),
            "liquidation": ("2000", "实时清算", "market", "market"),
            "wallet-unsupported": ("2026", "聪明钱监控", "wallet", "market"),
            "market-unsupported": ("2083", "Large-scale liquidation", "market", "market"),
            "sent-unsupported": ("2083", "Large-scale liquidation", "market", "market"),
            "queued-push-unsupported": ("2026", "聪明钱监控", "wallet", "market"),
            "dropped-unsupported": ("2026", "聪明钱监控", "wallet", "market"),
            "rebound-known-id": ("1019", "Rebound monitor", "market", "market"),
            "ordinary-news": ("2082", "Organizational Changes", "news", "news"),
            "focused-unsupported": ("1018", "News Score > 70", "news", "news"),
            "judged-before-focus": ("1018", "News Score > 70", "news", "news"),
            "generic-judged-oi-focus": ("1018", "News Score > 70", "news", "news"),
            "ordinary-listing": ("9998", "Another listing", "news", "listing"),
            "scored-market": ("9997", "Rated market signal", "market", "market"),
            "unknown-market": ("9996", "Unknown market signal", "market", "market"),
            "unknown-engine-market": ("9995", "Unknown engine market signal", "news", "market"),
        }
        for event_id, strategy in rows.items():
            _seed_event(
                conn,
                event_id,
                strategy=strategy,
                score=85 if event_id == "scored-market" else None,
                admission="candidate"
                if event_id
                in {
                    "sent-unsupported",
                    "queued-push-unsupported",
                    "dropped-unsupported",
                    "focused-unsupported",
                    "judged-before-focus",
                    "generic-judged-oi-focus",
                }
                else "suppressed_low_signal",
                ingest_mode="recovery" if event_id == "legacy-recovery-oi" else "live",
            )
        focused_metadata = {
            "strategies": [
                {
                    "id": "2083",
                    "name": "Large-scale liquidation",
                    "source_type": "market",
                    "engine_type": "market",
                }
            ]
        }
        conn.execute(
            """
            INSERT INTO news_items (
              item_id, source_id, source_item_key, title, published_at_ms, observed_at_ms,
              provider_metadata, first_ingest_mode, created_at_ms, updated_at_ms
            ) VALUES (
              'i-focused-unsupported-member', 'opennews', 'k-focused-unsupported-member',
              'Focused unsupported member', %s, %s, %s::jsonb, 'live', %s, %s
            )
            """,
            (NOW, NOW, json.dumps(focused_metadata), NOW, NOW),
        )
        conn.execute(
            """
            INSERT INTO news_event_members (
              event_id, item_id, joined_at_ms, match_kind, jaccard_estimate, fact_id, fact_text
            ) VALUES (
              'focused-unsupported', 'i-focused-unsupported-member', %s,
              'exact', 1.0, 'fact:focused-unsupported-member', 'Focused unsupported member'
            )
            """,
            (NOW + 1,),
        )
        conn.execute(
            """
            INSERT INTO news_event_evidence_snapshots (
              event_id, evidence_version, focus_fact_id, evidence_sha256,
              provenance, release_eligible, snapshot, created_at_ms
            ) VALUES (
              'focused-unsupported', 1, 'fact:focused-unsupported-member', %s,
              'observed', true, %s::jsonb, %s
            )
            """,
            (
                "c" * 64,
                json.dumps(
                    {
                        "schema_version": "news_event_evidence_v2",
                        "event_id": "focused-unsupported",
                        "focus_fact": {"fact_id": "fact:focused-unsupported-member"},
                        "card": {
                            "leader_item_id": "i-focused-unsupported-member",
                            "provider_metadata": focused_metadata,
                        },
                        "members": [],
                        "provenance": "observed",
                    }
                ),
                NOW + 1,
            ),
        )
        conn.execute(
            """
            INSERT INTO news_items (
              item_id, source_id, source_item_key, title, published_at_ms, observed_at_ms,
              provider_metadata, first_ingest_mode, created_at_ms, updated_at_ms
            ) VALUES (
              'i-judged-before-focus-member', 'opennews', 'k-judged-before-focus-member',
              'Later unsupported member', %s, %s, %s::jsonb, 'live', %s, %s
            )
            """,
            (NOW, NOW, json.dumps(focused_metadata), NOW, NOW),
        )
        conn.execute(
            """
            INSERT INTO news_event_members (
              event_id, item_id, joined_at_ms, match_kind, jaccard_estimate, fact_id, fact_text
            ) VALUES (
              'judged-before-focus', 'i-judged-before-focus-member', %s,
              'exact', 1.0, 'fact:judged-before-focus-member', 'Later unsupported member'
            )
            """,
            (NOW + 2,),
        )
        leader_metadata = {
            "strategies": [{"id": "1018", "name": "News Score > 70", "source_type": "news", "engine_type": "news"}]
        }
        for version, fact_id, item_id, metadata, evidence_sha in (
            (1, "fact:judged-before-focus", "i-judged-before-focus", leader_metadata, "d" * 64),
            (
                2,
                "fact:judged-before-focus-member",
                "i-judged-before-focus-member",
                focused_metadata,
                "e" * 64,
            ),
        ):
            conn.execute(
                """
                INSERT INTO news_event_evidence_snapshots (
                  event_id, evidence_version, focus_fact_id, evidence_sha256,
                  provenance, release_eligible, snapshot, created_at_ms
                ) VALUES (
                  'judged-before-focus', %s, %s, %s, 'observed', true, %s::jsonb, %s
                )
                """,
                (
                    version,
                    fact_id,
                    evidence_sha,
                    json.dumps(
                        {
                            "schema_version": "news_event_evidence_v2",
                            "event_id": "judged-before-focus",
                            "focus_fact": {"fact_id": fact_id},
                            "card": {"leader_item_id": item_id, "provider_metadata": metadata},
                            "members": [],
                            "provenance": "observed",
                        }
                    ),
                    NOW + version,
                ),
            )
        conn.execute(
            """
            INSERT INTO news_verdicts (
              event_id, stage, policy_version, rule_baseline_decision, final_decision,
              verdict, degraded, trace, published_at_ms, created_at_ms,
              evidence_version, evidence_sha256, focus_fact_id
            ) VALUES (
              'judged-before-focus', 'triage', 'legacy-v6', 'push', 'push', '{}'::jsonb,
              false, '{}'::jsonb, %s, %s, 1, %s, 'fact:judged-before-focus'
            )
            """,
            (NOW, NOW, "d" * 64),
        )
        oi_focus_metadata = {
            "strategies": [
                {
                    "id": "1019",
                    "name": "OI Event Monitor",
                    "source_type": "market",
                    "engine_type": "market",
                }
            ]
        }
        conn.execute(
            """
            INSERT INTO news_items (
              item_id, source_id, source_item_key, title, published_at_ms, observed_at_ms,
              provider_metadata, first_ingest_mode, created_at_ms, updated_at_ms
            ) VALUES (
              'i-generic-judged-oi-focus-member', 'opennews', 'k-generic-judged-oi-focus-member',
              'OI-shaped focus judged generically', %s, %s, %s::jsonb, 'live', %s, %s
            )
            """,
            (NOW, NOW, json.dumps(oi_focus_metadata), NOW, NOW),
        )
        conn.execute(
            """
            INSERT INTO news_event_members (
              event_id, item_id, joined_at_ms, match_kind, jaccard_estimate, fact_id, fact_text
            ) VALUES (
              'generic-judged-oi-focus', 'i-generic-judged-oi-focus-member', %s,
              'exact', 1.0, 'fact:generic-judged-oi-focus-member', 'OI-shaped focus judged generically'
            )
            """,
            (NOW + 1,),
        )
        conn.execute(
            """
            INSERT INTO news_event_evidence_snapshots (
              event_id, evidence_version, focus_fact_id, evidence_sha256,
              provenance, release_eligible, snapshot, created_at_ms
            ) VALUES (
              'generic-judged-oi-focus', 1, 'fact:generic-judged-oi-focus-member', %s,
              'observed', true, %s::jsonb, %s
            )
            """,
            (
                "f" * 64,
                json.dumps(
                    {
                        "schema_version": "news_event_evidence_v2",
                        "event_id": "generic-judged-oi-focus",
                        "focus_fact": {"fact_id": "fact:generic-judged-oi-focus-member"},
                        "card": {
                            "leader_item_id": "i-generic-judged-oi-focus-member",
                            "provider_metadata": oi_focus_metadata,
                        },
                        "members": [],
                        "provenance": "observed",
                    }
                ),
                NOW + 1,
            ),
        )
        conn.execute(
            """
            INSERT INTO news_verdicts (
              event_id, stage, policy_version, rule_baseline_decision, final_decision,
              verdict, degraded, trace, published_at_ms, created_at_ms,
              evidence_version, evidence_sha256, focus_fact_id, program_version, program_sha256
            ) VALUES (
              'generic-judged-oi-focus', 'triage', 'legacy-v6', 'push', 'push', '{}'::jsonb,
              false, '{}'::jsonb, %s, %s, 1, %s, 'fact:generic-judged-oi-focus-member',
              'news_semantic_program_v5', %s
            )
            """,
            (NOW, NOW, "f" * 64, "a" * 64),
        )
        # A pre-cut Item may already contain the first-seen union from two
        # Strategy deliveries. The hard-cut migration conservatively keeps its
        # one historical Event; current admission can rebuild the additional kind.
        conn.execute(
            """
            UPDATE news_items
               SET provider_metadata = jsonb_set(
                 provider_metadata,
                 '{strategies}',
                 (provider_metadata -> 'strategies') ||
                 '[{"id":"1019","name":"OI Event Monitor","source_type":"market","engine_type":"market"}]'::jsonb
               )
             WHERE item_id = 'i-ordinary-news'
            """
        )
        oi_strategy = ("1019", "OI Event Monitor", "market", "market")
        for event_id, record_id, symbol in (
            ("pending-same-item-oi", "2881001", "SAME"),
            ("pending-cross-item-oi", "2881002", "CROSS"),
        ):
            title = f"{symbol} OI Rise 4.55%, OI Value 32.17M, Whale Long Profit 80.21%, Whale/OI Ratio 100.71%"
            item_id = item_identity(source_id=OPENNEWS_SOURCE_ID, source_item_key=record_id)
            fact = extract_fact_units(item_id=item_id, raw_text=title, fallback_title=title)[0]
            comparison = extract_title(title).comparison
            _seed_event(
                conn,
                event_id,
                strategy=oi_strategy,
                admission="telemetry_deterministic",
                item_id=item_id,
                source_item_key=record_id,
                title=title,
                fact_id=fact.fact_id,
                comparison_fingerprint=hashlib.sha256(comparison.encode()).hexdigest(),
                family=dedupe_family(comparison),
            )
        legacy_collision_record_id = "2881004"
        legacy_collision_title = (
            "Market digest\n"
            "1. COLLISION OI Rise 4.55%, OI Value 32.17M, "
            "Whale Long Profit 80.21%, Whale/OI Ratio 100.71%\n"
            "2. BTC spot volume rose while funding stayed flat\n"
            "3. ETH options skew narrowed into the close"
        )
        legacy_collision_item_id = item_identity(
            source_id=OPENNEWS_SOURCE_ID,
            source_item_key=legacy_collision_record_id,
        )
        legacy_collision_fact = extract_fact_units(
            item_id=legacy_collision_item_id,
            raw_text=legacy_collision_title,
            fallback_title=legacy_collision_title,
        )[0]
        assert legacy_collision_fact.method == "explicit_numbered"
        legacy_collision_event_id = hashlib.sha256(
            f"{legacy_collision_item_id}\x1f{legacy_collision_fact.fact_id}".encode()
        ).hexdigest()
        legacy_collision_news_event_id = hashlib.sha256(
            f"{legacy_collision_item_id}\x1f{legacy_collision_fact.fact_id}\x1fnews".encode()
        ).hexdigest()
        legacy_collision_comparison = extract_title(legacy_collision_fact.text).comparison
        _seed_event(
            conn,
            legacy_collision_event_id,
            strategy=oi_strategy,
            admission="telemetry_deterministic",
            item_id=legacy_collision_item_id,
            source_item_key=legacy_collision_record_id,
            title=legacy_collision_title,
            fact_id=legacy_collision_fact.fact_id,
            comparison_fingerprint=hashlib.sha256(legacy_collision_comparison.encode()).hexdigest(),
            family=dedupe_family(legacy_collision_comparison),
        )
        for event_id, admission in {
            "typed-liquidation": "candidate",
            "admission-liquidation": "liquidation_deterministic",
            "admission-oi": "telemetry_deterministic",
            "admission-listing": "listing_deterministic",
            "admission-unsupported": "unsupported_market_contract",
        }.items():
            _seed_event(conn, event_id, strategy=None, admission=admission)
        conn.execute(
            """
            INSERT INTO news_market_liquidations (
              source_key, item_id, fact_id, ingest_mode, symbol, venue, liquidated_position_side,
              forced_order_side, notional_usd, quantity, price, event_at_ms, received_at_ms,
              parser_version, provider_record_identity, symbol_contract_identity,
              position_side_semantics, quantity_semantics, notional_semantics, price_semantics,
              completeness_assumption, throttle_assumption, source_contract_version,
              source_contract_complete, created_at_ms
            ) VALUES (
              'liq:typed', 'i-typed-liquidation', 'fact:typed-liquidation', 'live', 'ETH', 'binance',
              'long', 'sell', 100000, 10, 10000, %s, %s, 'liquidation_parser_v1', 'record',
              'ETH:perp', 'position_side', 'quantity', 'notional', 'price', 'complete', 'provider',
              'opennews_liquidation_source_v1', true, %s
            )
            """,
            (NOW, NOW, NOW),
        )
        # A complete ordinary-News tuple remains authoritative even when a
        # later secondary Strategy left a typed liquidation fact on the Item.
        conn.execute(
            """
            INSERT INTO news_market_liquidations (
              source_key, item_id, fact_id, ingest_mode, symbol, venue, liquidated_position_side,
              forced_order_side, notional_usd, quantity, price, event_at_ms, received_at_ms,
              parser_version, provider_record_identity, symbol_contract_identity,
              position_side_semantics, quantity_semantics, notional_semantics, price_semantics,
              completeness_assumption, throttle_assumption, source_contract_version,
              source_contract_complete, created_at_ms
            )
            SELECT 'liq:ordinary-secondary', 'i-ordinary-news', 'fact:ordinary-news', ingest_mode,
                   symbol, venue, liquidated_position_side, forced_order_side, notional_usd, quantity,
                   price, event_at_ms, received_at_ms, parser_version, 'ordinary-secondary',
                   symbol_contract_identity, position_side_semantics, quantity_semantics,
                   notional_semantics, price_semantics, completeness_assumption, throttle_assumption,
                   source_contract_version, source_contract_complete, created_at_ms
              FROM news_market_liquidations WHERE source_key = 'liq:typed'
            """
        )
        # One typed liquidation fact proves only its own FactUnit on a split
        # Item; a sibling deterministic Event must remain unverified.
        conn.execute(
            """
            INSERT INTO news_events (
              event_id, leader_item_id, family, comparison_fingerprint, comparison_title, leader_title,
              focus_fact_id, opened_at_ms, last_member_at_ms, expires_at_ms, admission, ingest_mode,
              created_at_ms, updated_at_ms
            ) VALUES (
              'typed-liquidation-sibling', 'i-typed-liquidation', 'general', 'fp-sibling',
              'sibling comparison', 'Sibling fact', 'fact:sibling', %s, %s, %s,
              'liquidation_deterministic', 'live', %s, %s
            )
            """,
            (NOW, NOW, NOW + 1, NOW, NOW),
        )
        conn.execute(
            """
            INSERT INTO news_event_members(
              event_id, item_id, joined_at_ms, match_kind, jaccard_estimate, fact_id, fact_text
            ) VALUES (
              'typed-liquidation-sibling', 'i-typed-liquidation', %s,
              'leader', NULL, 'fact:sibling', 'Sibling fact'
            )
            """,
            (NOW,),
        )
        conn.execute(
            """
            INSERT INTO news_canary_activations (
              activation_id, baseline_bundle_sha, candidate_manifest_sha, candidate_bundle_sha,
              selector_version, exposure_bps, eligibility_profile_sha, rolling_profile_sha,
              state, revision, created_at_ms, activated_at_ms
            ) VALUES (%s, %s, %s, %s, 'news_canary_selector_v1', 1000, %s, %s, 'active', 2, %s, %s)
            """,
            ("b" * 32, "1" * 64, "2" * 64, "3" * 64, "4" * 64, "5" * 64, NOW, NOW),
        )
        conn.execute(
            """
            INSERT INTO news_verdicts (
              event_id, stage, policy_version, rule_baseline_decision, final_decision,
              verdict, degraded, error_code, trace, created_at_ms
            ) VALUES
              ('oi', 'triage', 'oi-v1', 'drop', 'drop', '{}'::jsonb, false,
               'oi_parse_failed', '{}'::jsonb, %s),
              ('liquidation', 'triage', 'liquidation-v1', 'drop', 'drop', '{}'::jsonb, false,
               'liquidation_parse_failed', '{}'::jsonb, %s)
            """,
            (NOW, NOW),
        )
        conn.execute(
            """
            INSERT INTO news_verdicts (
              event_id, stage, policy_version, rule_baseline_decision, final_decision,
              verdict, degraded, trace, created_at_ms
            ) VALUES
              ('sent-unsupported', 'triage', 'legacy-v6', 'push', 'push',
               '{"direction":"bullish","magnitude":1}'::jsonb, false, '{}'::jsonb, %s),
              ('queued-push-unsupported', 'triage', 'legacy-v6', 'push', 'push',
               '{"direction":"bullish","magnitude":1}'::jsonb, false, '{}'::jsonb, %s),
              ('dropped-unsupported', 'triage', 'legacy-v6', 'drop', 'drop',
               '{"direction":"neutral","magnitude":0}'::jsonb, false, '{}'::jsonb, %s)
            """,
            (NOW, NOW, NOW),
        )
        conn.execute(
            """
            INSERT INTO news_deliveries (
              event_id, kind, state, card, attempted_at_ms, settled_at_ms, created_at_ms
            ) VALUES ('sent-unsupported', 'first', 'sent', '{}'::jsonb, %s, %s, %s)
            """,
            (NOW, NOW, NOW),
        )
        conn.commit()
        conn.close()
        conn = None

        _upgrade("head")

        conn = connect_postgres_test(read_only=False)
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"] == "20260829_0340"
        assert {
            str(row["event_id"]): str(row["event_kind"])
            for row in conn.execute("SELECT event_id, event_kind FROM news_events ORDER BY event_id").fetchall()
        } == {
            "liquidation": "liquidation",
            legacy_collision_event_id: "oi",
            "legacy-recovery-oi": "oi",
            "listing": "listing",
            "market-unsupported": "unsupported_market",
            "oi": "oi",
            "admission-liquidation": "liquidation",
            "admission-listing": "listing",
            "admission-oi": "oi",
            "admission-unsupported": "unsupported_market",
            "dropped-unsupported": "unsupported_market",
            "focused-unsupported": "unsupported_market",
            "generic-judged-oi-focus": "news",
            "judged-before-focus": "news",
            "ordinary-listing": "listing",
            "ordinary-news": "news",
            "pending-cross-item-oi": "oi",
            "pending-same-item-oi": "oi",
            "queued-push-unsupported": "unsupported_market",
            "rebound-known-id": "unsupported_market",
            "scored-market": "news",
            "sent-unsupported": "unsupported_market",
            "typed-liquidation": "news",
            "typed-liquidation-sibling": "liquidation",
            "unknown-market": "unsupported_market",
            "unknown-engine-market": "unsupported_market",
            "wallet-unsupported": "unsupported_market",
        }
        assert {
            str(row["event_id"]): str(row["admission"])
            for row in conn.execute(
                "SELECT event_id, admission FROM news_events WHERE event_kind = 'unsupported_market' ORDER BY event_id"
            ).fetchall()
        } == {
            "admission-unsupported": "unsupported_market_contract",
            "market-unsupported": "unsupported_market_contract",
            "dropped-unsupported": "unsupported_market_contract",
            "focused-unsupported": "unsupported_market_contract",
            "queued-push-unsupported": "unsupported_market_contract",
            "rebound-known-id": "unsupported_market_contract",
            "sent-unsupported": "unsupported_market_contract",
            "unknown-engine-market": "unsupported_market_contract",
            "unknown-market": "unsupported_market_contract",
            "wallet-unsupported": "unsupported_market_contract",
        }
        assert {
            str(row["event_id"]): row["source_contract_reason"]
            for row in conn.execute(
                "SELECT event_id, source_contract_reason FROM news_events ORDER BY event_id"
            ).fetchall()
        } == {
            "admission-liquidation": "source_contract_unverified",
            "admission-listing": None,
            "admission-oi": "source_contract_unverified",
            "admission-unsupported": "unsupported_market_contract",
            "dropped-unsupported": "unsupported_market_contract",
            "focused-unsupported": "unsupported_market_contract",
            "generic-judged-oi-focus": None,
            "judged-before-focus": None,
            "liquidation": "source_contract_drift",
            legacy_collision_event_id: "source_contract_unverified",
            "legacy-recovery-oi": "source_contract_unverified",
            "listing": None,
            "market-unsupported": "unsupported_market_contract",
            "oi": "source_contract_drift",
            "ordinary-listing": None,
            "ordinary-news": None,
            "pending-cross-item-oi": "source_contract_unverified",
            "pending-same-item-oi": "source_contract_unverified",
            "queued-push-unsupported": "unsupported_market_contract",
            "rebound-known-id": "source_contract_drift",
            "scored-market": None,
            "sent-unsupported": "unsupported_market_contract",
            "typed-liquidation": None,
            "typed-liquidation-sibling": "source_contract_unverified",
            "unknown-engine-market": "unsupported_market_contract",
            "unknown-market": "unsupported_market_contract",
            "wallet-unsupported": "unsupported_market_contract",
        }
        news = repositories_for_connection(conn).news
        assert news.event_detail("sent-unsupported") == {"archive_only": True}
        assert news.event_detail("queued-push-unsupported") == {"archive_only": True}
        assert news.event_detail("dropped-unsupported") == {"archive_only": True}
        assert (
            conn.execute("SELECT count(*) AS n FROM news_events WHERE leader_item_id = 'i-ordinary-news'").fetchone()[
                "n"
            ]
            == 1
        )

        def _oi_frame(record_id: str, symbol: str):
            title = f"{symbol} OI Rise 4.55%, OI Value 32.17M, Whale Long Profit 80.21%, Whale/OI Ratio 100.71%"
            parsed = parse_opennews_message(
                {
                    "method": "strategy.triggered",
                    "params": {
                        "id": record_id,
                        "text": title,
                        "source": "binance",
                        "engineType": "market",
                        "ts": NOW,
                        "strategy": {"id": 1019, "name": "OI Event Monitor", "sourceType": "market"},
                    },
                }
            )
            assert parsed is not None
            return parsed

        repos = repositories_for_connection(conn)
        with repos.transaction():
            same_item = admit_item(
                repos,
                event=_oi_frame("2881001", "SAME"),
                ingest_mode="live",
                observed_at_ms=NOW,
                trace_id="post-cut-same-item-redelivery",
                watchlist_symbols=frozenset(),
                now_ms=NOW,
            )
            cross_item = admit_item(
                repos,
                event=_oi_frame("2881003", "CROSS"),
                ingest_mode="live",
                observed_at_ms=NOW,
                trace_id="post-cut-cross-item-redelivery",
                watchlist_symbols=frozenset(),
                now_ms=NOW,
            )
        assert same_item.event_created and same_item.event_id != "pending-same-item-oi"
        assert cross_item.event_created and cross_item.event_id != "pending-cross-item-oi"
        assert same_item.event_id != cross_item.event_id
        ordinary_redelivery = parse_opennews_message(
            {
                "method": "strategy.triggered",
                "params": {
                    "id": legacy_collision_record_id,
                    "text": legacy_collision_title,
                    "source": "wire",
                    "engineType": "news",
                    "score": 90,
                    "ts": NOW,
                    "strategy": {"id": 1018, "name": "News Score > 70", "sourceType": "news"},
                },
            }
        )
        assert ordinary_redelivery is not None
        with repos.transaction():
            ordinary = admit_item(
                repos,
                event=ordinary_redelivery,
                ingest_mode="live",
                observed_at_ms=NOW,
                trace_id="post-cut-news-legacy-id-collision",
                watchlist_symbols=frozenset(),
                now_ms=NOW,
            )
        assert ordinary.event_kind == "news" and ordinary.event_created
        assert ordinary.event_id != legacy_collision_news_event_id
        assert {
            row["event_kind"]
            for row in conn.execute(
                """
                SELECT DISTINCT e.event_kind
                  FROM news_event_members m JOIN news_events e ON e.event_id = m.event_id
                 WHERE m.item_id = %s
                """,
                (legacy_collision_item_id,),
            ).fetchall()
        } == {"news", "oi"}
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM news_events WHERE event_id IN "
                "('pending-same-item-oi', 'pending-cross-item-oi')"
            ).fetchone()["n"]
            == 2
        )
        assert (
            conn.execute(
                "SELECT count(DISTINCT item_id) AS n FROM news_event_members WHERE event_id = 'pending-cross-item-oi'"
            ).fetchone()["n"]
            == 1
        )
        assert {
            row["event_id"]: row["source_contract_reason"]
            for row in conn.execute(
                "SELECT event_id, source_contract_reason FROM news_events "
                "WHERE event_id IN ('pending-same-item-oi', 'pending-cross-item-oi')"
            ).fetchall()
        } == {
            "pending-same-item-oi": "source_contract_unverified",
            "pending-cross-item-oi": "source_contract_unverified",
        }
        assert {
            row["event_id"]: row["source_contract_reason"]
            for row in conn.execute(
                "SELECT event_id, source_contract_reason FROM news_current_events_v1 "
                "WHERE event_id = ANY(%s) ORDER BY event_id",
                ([same_item.event_id, cross_item.event_id],),
            ).fetchall()
        } == {same_item.event_id: None, cross_item.event_id: None}

        contracts = news.status_snapshot(now_ms=NOW + 1)["pipeline"]["source_contracts_24h"]
        assert contracts["oi_v1"] == {
            "received": 2,
            "parsed": 2,
            "parse_failed": 0,
            "unsupported": 0,
            "verdict": 0,
        }
        assert contracts["liquidation_v1"] == {
            "received": 0,
            "parsed": 0,
            "parse_failed": 0,
            "unsupported": 0,
            "verdict": 0,
        }

        columns = {
            str(row["column_name"]): str(row["is_nullable"])
            for row in conn.execute(
                "SELECT column_name, is_nullable FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='news_events' "
                "AND column_name IN ('event_kind', 'source_contract_reason')"
            ).fetchall()
        }
        assert columns == {"event_kind": "NO", "source_contract_reason": "YES"}
        indexes = {
            str(row["indexname"]): str(row["indexdef"])
            for row in conn.execute(
                "SELECT indexname, indexdef FROM pg_indexes "
                "WHERE schemaname='public' AND tablename='news_events' AND indexname='ix_news_events_kind_opened'"
            ).fetchall()
        }
        assert "event_kind, opened_at_ms DESC, event_id DESC" in indexes["ix_news_events_kind_opened"]
        unpublished = conn.execute(
            "SELECT indexdef FROM pg_indexes WHERE schemaname='public' AND tablename='news_events' "
            "AND indexname='ix_news_events_unpublished'"
        ).fetchone()["indexdef"]
        assert "published_at_ms IS NULL" in unpublished
        assert "liquidation_deterministic" in unpublished

        conn.execute("BEGIN")
        conn.execute("SAVEPOINT invalid_kind")
        with pytest.raises(CheckViolation, match="news_events_event_kind_check"):
            conn.execute("UPDATE news_events SET event_kind='wallet' WHERE event_id=%s", (ordinary.event_id,))
        conn.execute("ROLLBACK TO SAVEPOINT invalid_kind")
        conn.execute("SAVEPOINT missing_kind")
        with pytest.raises(NotNullViolation):
            conn.execute("UPDATE news_events SET event_kind=NULL WHERE event_id=%s", (ordinary.event_id,))
        conn.execute("ROLLBACK TO SAVEPOINT missing_kind")
        conn.execute("SAVEPOINT invalid_reason")
        with pytest.raises(CheckViolation, match=r"news_events_source_contract_(reason|consistency)_check"):
            conn.execute(
                "UPDATE news_events SET source_contract_reason='parser_unknown' WHERE event_id=%s",
                (ordinary.event_id,),
            )
        conn.execute("ROLLBACK TO SAVEPOINT invalid_reason")
        conn.execute("SAVEPOINT current_unverified_reason")
        with pytest.raises(CheckViolation, match=r"news_events_source_contract_(reason|consistency)_check"):
            conn.execute(
                "UPDATE news_events SET source_contract_reason='source_contract_unverified' WHERE event_id=%s",
                (same_item.event_id,),
            )
        conn.execute("ROLLBACK TO SAVEPOINT current_unverified_reason")
        conn.execute("SAVEPOINT unsupported_missing_reason")
        with pytest.raises(CheckViolation, match="news_events_source_contract_consistency_check"):
            conn.execute(
                "UPDATE news_events SET event_kind='unsupported_market', source_contract_reason=NULL WHERE event_id=%s",
                (ordinary.event_id,),
            )
        conn.execute("ROLLBACK TO SAVEPOINT unsupported_missing_reason")
        conn.execute("SAVEPOINT news_with_drift_reason")
        with pytest.raises(CheckViolation, match="news_events_source_contract_consistency_check"):
            conn.execute(
                "UPDATE news_events SET source_contract_reason='source_contract_drift' WHERE event_id=%s",
                (ordinary.event_id,),
            )
        conn.execute("ROLLBACK TO SAVEPOINT news_with_drift_reason")
        conn.execute("SAVEPOINT oi_with_unsupported_reason")
        with pytest.raises(CheckViolation, match="news_events_source_contract_consistency_check"):
            conn.execute(
                "UPDATE news_events SET source_contract_reason='unsupported_market_contract' WHERE event_id=%s",
                (same_item.event_id,),
            )
        conn.execute("ROLLBACK TO SAVEPOINT oi_with_unsupported_reason")

        activation = conn.execute(
            "SELECT state, revision, trip_reason FROM news_canary_activations WHERE activation_id=%s",
            ("b" * 32,),
        ).fetchone()
        assert activation == {
            "state": "tripped",
            "revision": 3,
            "trip_reason": "news_source_contract_event_kind_hard_cut",
        }
        receipt = conn.execute(
            "SELECT payload, created_by FROM news_learning_artifacts WHERE created_by='migration_20260827_0315'"
        ).fetchone()
        assert receipt["payload"] == {
            "kind": "news_source_contract_event_kind_hard_cut",
            "source_issue": "https://github.com/AnalyThothAI/tracefold/issues/288",
            "epoch_id": "program_v7",
            "from_program_factory_id": "tracefold.news.program.factory_v6",
            "to_program_factory_id": "tracefold.news.program.factory_v7",
            "program_version": "news_semantic_program_v5",
            "event_identity_version": "news_event_identity_v5",
            "prior_evidence_disposition": "prior_factory_evidence_audit_only",
            "activation_disposition": "open_activations_tripped",
        }
        assert receipt["created_by"] == "migration_20260827_0315"
    finally:
        if conn is not None:
            conn.close()
        restore = connect_postgres_test(read_only=False)
        try:
            reset_postgres_schema(restore)
        finally:
            restore.close()


def test_0328_limits_generic_taxonomy_review_to_ordinary_news() -> None:
    conn: Any | None = None
    try:
        _fresh_schema_at("20260829_0326")
        setup = connect_postgres_test(read_only=False)
        try:
            setup.execute(
                """
                INSERT INTO news_canary_activations (
                  activation_id, baseline_bundle_sha, candidate_manifest_sha, candidate_bundle_sha,
                  selector_version, exposure_bps, eligibility_profile_sha, rolling_profile_sha,
                  state, revision, created_at_ms, activated_at_ms
                ) VALUES (%s, %s, %s, %s, 'news_canary_selector_v2', 1000, %s, %s, 'active', 4, %s, %s)
                """,
                ("c" * 32, "1" * 64, "2" * 64, "3" * 64, "4" * 64, "5" * 64, NOW, NOW),
            )
            setup.commit()
        finally:
            setup.close()
        _upgrade("20260829_0328")
        conn = connect_postgres_test(read_only=False)

        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"] == "20260829_0328"
        columns = [
            row["column_name"]
            for row in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='news_review_task_source_v1' "
                "ORDER BY ordinal_position"
            ).fetchall()
        ]
        definition = conn.execute(
            "SELECT pg_get_viewdef('news_review_task_source_v1'::regclass, true) AS definition"
        ).fetchone()["definition"]

        assert columns[-1] == "event_kind"
        assert "event_kind" in definition and "'news'" in definition
        assert (
            conn.execute(
                "SELECT has_table_privilege('tracefold_serve', 'news_review_task_source_v1', 'SELECT') AS allowed"
            ).fetchone()["allowed"]
            is True
        )
        assert conn.execute(
            "SELECT state, revision, trip_reason FROM news_canary_activations WHERE activation_id=%s",
            ("c" * 32,),
        ).fetchone() == {
            "state": "tripped",
            "revision": 5,
            "trip_reason": "news_taxonomy_v1_hard_cut",
        }
        receipt = conn.execute(
            "SELECT payload, created_by FROM news_learning_artifacts WHERE created_by='migration_20260829_0328'"
        ).fetchone()
        assert receipt["payload"] == {
            "kind": "news_taxonomy_v1_hard_cut",
            "source_issue": "https://github.com/AnalyThothAI/tracefold/issues/117",
            "program_version": TAXONOMY_V1_PROGRAM_VERSION,
            "program_sha256": TAXONOMY_V1_PROGRAM_SHA256,
            "envelope_sha256": TAXONOMY_V1_ENVELOPE_SHA256,
            "taxonomy_version": TAXONOMY_V1_VERSION,
            "codebook_sha256": TAXONOMY_V1_CODEBOOK_SHA256,
            "review_rubric_version": TAXONOMY_V1_REVIEW_RUBRIC_VERSION,
            "prior_evidence_disposition": "news_review_v4_and_prior_program_evidence_audit_only",
            "runtime_epoch_disposition": "new_bundle_epoch_opened_by_worker_startup",
            "activation_disposition": "open_activations_tripped",
        }
        assert receipt["created_by"] == "migration_20260829_0328"
    finally:
        if conn is not None:
            conn.close()
        restore = connect_postgres_test(read_only=False)
        try:
            reset_postgres_schema(restore)
        finally:
            restore.close()
