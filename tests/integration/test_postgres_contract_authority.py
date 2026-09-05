from __future__ import annotations

import copy
from typing import Any, Final

import pytest
from psycopg.types.json import Jsonb
from pydantic import TypeAdapter, ValidationError

from tests.postgres_test_utils import connect_postgres_test
from tests.support.news_judgment import news_taxonomy
from tracefold.news.artifact_identity import canonical_sha
from tracefold.news.models import TriageVerdict
from tracefold.news.program.contracts import EditorialEnvelope, TradeRelevanceV1
from tracefold.news.review.desk import BlindPairwiseSubmission, EventRubricSubmission, _pairwise_virtual

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("postgres_clone_dsn")]


def _python_persisted_form_accepts(model: Any, payload: dict[str, Any]) -> bool:
    """Match the exact JSON shape the application writes after Pydantic materialization."""

    try:
        materialized = model.model_validate(payload).model_dump(mode="json")
    except ValidationError:
        return False
    return bool(materialized == payload)


def _python_dataclass_form_accepts(adapter: TypeAdapter[Any], payload: dict[str, Any]) -> bool:
    try:
        materialized = adapter.dump_python(adapter.validate_python(payload), mode="json")
    except ValidationError:
        return False
    return bool(materialized == payload)


# The exact market judgment shapes `news_verdicts_current_judgment_check` validated while market
# frames still wore a verdict, frozen as literals. #553 deleted the writers, and the four validators
# below now guard historical rows only -- so they are pinned against what was actually stored rather
# than against the live dataclasses, which have since gained a native instrument token, a reporting
# Strategy and a nullable venue. Comparing a historical validator with a current dataclass would
# either force the validator to reject rows it must keep accepting, or force the dataclass to carry
# fields the market plane no longer uses.
_HISTORICAL_OI_SIGNAL: Final[dict[str, Any]] = {
    "symbol": "SOL",
    "direction": "rise",
    "oi_change_bps": 455,
    "oi_value_usd": 32_170_000,
    "whale_long_profit_bps": 8021,
    "whale_oi_ratio_bps": 10_071,
}
_HISTORICAL_OI_METADATA: Final[dict[str, Any]] = {
    "parsed": True,
    "source_strategy_id": "1019",
    "source_contract_version": "opennews_oi_source_v1",
    "measurement_window_ms": 300_000,
    "source_contract_rule": "proven",
    "parser_version": "oi_signal_parser_v1",
    "source_classifier_version": "opennews_source_classifier_v1",
}
_HISTORICAL_LIQUIDATION_FACT: Final[dict[str, Any]] = {
    "source_key": "a" * 64,
    "item_id": "item-current",
    "fact_id": "fact-current",
    "symbol": "BTC",
    "venue": "binance",
    "liquidated_position_side": "short",
    "forced_order_side": "buy",
    "notional_usd": "1250000",
    "quantity": None,
    "price": "65000",
    "event_at_ms": 1_000,
    "received_at_ms": 1_100,
    "provider_record_identity": "item-current",
    "symbol_contract_identity": "unresolved:binance:BTC",
    "position_side_semantics": "template_position_side;short=>forced_buy;long=>forced_sell",
    "quantity_semantics": "not_provided",
    "notional_semantics": "provider_reported_usd_notional",
    "price_semantics": "provider_reported_unspecified_price",
    "completeness_assumption": "selected_events_without_heartbeat_sequence_or_coverage_sla",
    "throttle_assumption": "provider_throttle_unknown",
    "source_contract_version": "opennews_liquidation_source_v1",
    "source_contract_complete": False,
    "parser_version": "liquidation_parser_v1",
}
_HISTORICAL_LIQUIDATION_METADATA: Final[dict[str, Any]] = {
    "parsed": True,
    "source_latency_ms": 100,
    "parser_version": "liquidation_parser_v1",
    "source_classifier_version": "opennews_source_classifier_v1",
}


def _current_review_payload(*, production_sized: bool = False) -> dict[str, Any]:
    evidence_refs = []
    note = ""
    dimensions = {
        "factual_fidelity": "pass",
        "taxonomy_subject_codes": "pass",
        "taxonomy_event_family": "pass",
        "taxonomy_change_state": "pass",
        "taxonomy_source_authority": "pass",
        "taxonomy_assertion_status": "pass",
    }
    expected: dict[str, Any] | None = None
    if production_sized:
        evidence_refs = [f"ref-{index:02d}-" + "x" * 493 for index in range(32)]
        note = "n" * 2_000
        dimensions |= {
            "magnitude": "fail",
            "direction": "fail",
            "asset_grounding": "fail",
            "trade_impact_breadth": "fail",
            "trade_tradability": "fail",
            "trade_surprise": "fail",
            "trade_development_delta": "fail",
            "trade_channels": "fail",
            "trade_affected_markets": "fail",
            "reader_value": "fail",
        }
        expected = {
            "magnitude": 3,
            "direction": "bullish",
            "assets": [{"symbol": f"ASSET{index:02d}".ljust(32, "X"), "role": "primary"} for index in range(16)],
            "trade_impact_breadth": "global_systemic",
            "trade_tradability": "second_order",
            "trade_surprise": "material_vs_expectation",
            "trade_development_delta": "material_detail",
            "trade_channels": ["rates", "liquidity", "risk_premium", "energy_supply"],
            "trade_affected_markets": ["crypto_broad", "us_equity_broad", "rates", "fx"],
            "reader_value": "escalate",
        }
    return EventRubricSubmission(
        should_push="should_hold",
        dimensions=dimensions,
        novelty={"judgment": "new_fact"},
        first_bad_owner="triage_prompt",
        evidence_refs=evidence_refs,
        expected=expected,
        taxonomy=news_taxonomy(
            event_family="regulatory_legal",
            change_state="reported",
            assertion_status="claimed",
            source_authority="reputable_secondary",
        ),
        note=note,
    ).model_dump(mode="json")


def _current_pairwise_selection(*, dataset_role: str) -> dict[str, Any]:
    return _pairwise_virtual(
        {
            "run_sha": "a" * 64,
            "case_id": f"case-{dataset_role}",
            "evidence_sha256": "b" * 64,
            "output_a": {"answer": "A"},
            "output_b": {"answer": "B"},
            "disclosure": {},
            "dataset_role": dataset_role,
        }
    ).selection


def test_news_current_json_validators_match_the_python_contract() -> None:
    verdict = TriageVerdict(
        novelty="new_fact",
        restates=-1,
        assets=[{"symbol": f"ASSET{index}", "market_type": "spot", "role": "mentioned"} for index in range(8)],
        direction="bullish",
        scope="sector",
        magnitude=3,
        confidence=1,
        audience="crypto",
        headline_zh="跨语言契约",
        why_zh="数据库必须拒绝绕过应用模型的同一无效当前事实。",
    ).model_dump(mode="json")
    editorial = EditorialEnvelope.issue(
        relevance=TradeRelevanceV1(
            impact_breadth="sector",
            tradability="direct",
            surprise="material_vs_expectation",
            development_delta="state_change",
            channels=("commodity_demand",),
            affected_markets=("us_equity_broad",),
            reader_value="realtime",
        ),
        taxonomy=news_taxonomy(
            event_family="regulatory_legal",
            change_state="reported",
            assertion_status="claimed",
            source_authority="reputable_secondary",
        ),
    ).model_dump(mode="json")
    verdict_corpus = [
        verdict,
        verdict | {"retired": True},
        verdict | {"direction": "sideways"},
        verdict | {"why_zh": "x" * 141},
        verdict | {"assets": [*verdict["assets"], verdict["assets"][0]]},
    ]
    verdict_corpus.extend({key: value for key, value in verdict.items() if key != removed} for removed in verdict)
    verdict_asset_extra = copy.deepcopy(verdict)
    verdict_asset_extra["assets"][0]["retired"] = True
    verdict_corpus.append(verdict_asset_extra)
    editorial_corpus = [
        editorial,
        editorial | {"retired": True},
        editorial | {"editorial_sha256": "0" * 64},
        editorial | {"editorial_origin": "operator"},
    ]
    editorial_corpus.extend({key: value for key, value in editorial.items() if key != removed} for removed in editorial)
    editorial_relevance_extra = copy.deepcopy(editorial)
    editorial_relevance_extra["relevance"]["retired"] = True
    editorial_corpus.append(editorial_relevance_extra)
    editorial_taxonomy_extra = copy.deepcopy(editorial)
    editorial_taxonomy_extra["taxonomy"]["retired"] = True
    editorial_corpus.append(editorial_taxonomy_extra)

    conn = connect_postgres_test(read_only=False)
    try:
        for payload in verdict_corpus:
            db_accepts = bool(
                conn.execute("SELECT news_current_triage_verdict_valid(%s) AS valid", (Jsonb(payload),)).fetchone()[
                    "valid"
                ]
            )
            assert db_accepts is _python_persisted_form_accepts(TriageVerdict, payload)
        for payload in editorial_corpus:
            db_accepts = bool(
                conn.execute("SELECT news_current_model_editorial_valid(%s) AS valid", (Jsonb(payload),)).fetchone()[
                    "valid"
                ]
            )
            assert db_accepts is _python_persisted_form_accepts(EditorialEnvelope, payload)
    finally:
        conn.close()


def test_news_canonical_json_hash_matches_python_for_nested_unicode_payload() -> None:
    payload = {
        "z": [3, {"中文": "证据", "boolean": True}, None],
        "a": {"nested": [2, 1], "value": -42},
    }
    conn = connect_postgres_test(read_only=False)
    try:
        row = conn.execute(
            "SELECT encode(sha256(convert_to(news_canonical_jsonb(%s), 'UTF8')), 'hex') AS sha",
            (Jsonb(payload),),
        ).fetchone()
    finally:
        conn.close()

    assert row["sha"] == canonical_sha(payload)


def test_retained_telemetry_and_review_validators_match_python_owned_shapes() -> None:
    """The review validator still mirrors its Python shape; the market ones still hold history.

    A market judgment is no longer written (#553), so the two market validators are asked one
    question here: do they still accept exactly the shape that is stored, and still refuse a drifted
    one. That is what keeps a later migration from quietly widening or dropping a rule that guards
    rows nothing can rewrite.
    """

    oi, oi_metadata = _HISTORICAL_OI_SIGNAL, _HISTORICAL_OI_METADATA
    liquidation, liquidation_metadata = _HISTORICAL_LIQUIDATION_FACT, _HISTORICAL_LIQUIDATION_METADATA
    review = _current_review_payload()
    oi_corpus = [(oi, True), (oi | {"retired": True}, False), (oi | {"symbol": ["SOL"]}, False)]
    oi_corpus.extend(({key: value for key, value in oi.items() if key != removed}, False) for removed in oi)
    liquidation_corpus = [
        (liquidation, True),
        (liquidation | {"retired": True}, False),
        (liquidation | {"venue": 7}, False),
    ]
    liquidation_corpus.extend(
        ({key: value for key, value in liquidation.items() if key != removed}, False) for removed in liquidation
    )
    taxonomy_drift = copy.deepcopy(review)
    taxonomy_drift["taxonomy"]["retired"] = True
    review_corpus = [review, review | {"retired": True}, taxonomy_drift]
    review_corpus.extend({key: value for key, value in review.items() if key != removed} for removed in review)
    selection = {
        "stratum": "random_control",
        "stratum_zh": "随机对照",
        "reason": "coverage_control",
        "reason_zh": "覆盖对照",
        "sampling_probability": 0.02,
        "selection_version": "news_review_sampler_v3",
    }

    conn = connect_postgres_test(read_only=False)
    try:
        for payload, expected_valid in oi_corpus:
            row = conn.execute(
                "SELECT news_current_oi_signal_valid(%s) AS valid",
                (Jsonb(payload),),
            ).fetchone()
            assert bool(row["valid"]) is expected_valid, payload
        for payload, expected_valid in liquidation_corpus:
            row = conn.execute(
                "SELECT news_current_liquidation_fact_valid(%s) AS valid",
                (Jsonb(payload),),
            ).fetchone()
            assert bool(row["valid"]) is expected_valid, payload
        for payload, function_name in (
            (oi_metadata, "news_current_oi_metadata_valid"),
            (liquidation_metadata, "news_current_liquidation_metadata_valid"),
        ):
            valid = conn.execute(
                f"SELECT {function_name}(%s, true) AS valid",
                (Jsonb(payload),),
            ).fetchone()
            drift = conn.execute(
                f"SELECT {function_name}(%s, true) AS valid",
                (Jsonb(payload | {"retired": True}),),
            ).fetchone()
            assert bool(valid["valid"]) is True
            assert bool(drift["valid"]) is False
        for index, payload in enumerate(review_corpus):
            row = conn.execute(
                """
                    SELECT news_current_review_valid(
                      'judgment', 'event', 'news_review_v6', 'reader_contract_v2',
                      'event-current', 1, NULL, NULL,
                      %(should_push)s, %(dimensions)s, %(novelty)s,
                      %(first_bad_owner)s, %(evidence_refs)s, %(expected_correction)s, %(note)s,
                      %(selection)s, %(payload)s, NULL
                    ) AS valid
                """,
                {
                    "payload": Jsonb(payload),
                    "should_push": payload.get("should_push"),
                    "dimensions": Jsonb(payload.get("dimensions")),
                    "novelty": Jsonb(payload.get("novelty")),
                    "first_bad_owner": payload.get("first_bad_owner"),
                    "evidence_refs": Jsonb(payload.get("evidence_refs")),
                    "expected_correction": payload.get("expected_correction"),
                    "note": payload.get("note"),
                    "selection": Jsonb(selection),
                },
            ).fetchone()
            assert bool(row["valid"]) is _python_persisted_form_accepts(EventRubricSubmission, payload), (
                index,
                payload,
            )
        pairwise = BlindPairwiseSubmission(
            preference="A",
            critical_errors=["A:unsupported_fact"],
            evidence_refs=["output:A"],
        ).model_dump(mode="json")
        pairwise_selection = _current_pairwise_selection(dataset_role="validation")
        pairwise_corpus = [
            pairwise,
            pairwise | {"retired": True},
            pairwise | {"preference": "winner"},
            {key: value for key, value in pairwise.items() if key != "evidence_refs"},
        ]
        for payload in pairwise_corpus:
            row = conn.execute(
                """
                SELECT news_current_review_valid(
                  'judgment', 'pairwise', 'news_review_v6', 'reader_contract_v2',
                  NULL, NULL, NULL, 'pairwise-current',
                  NULL, '{}'::jsonb, '{}'::jsonb, NULL, %(evidence_refs)s, '', %(note)s,
                  %(selection)s, %(payload)s, NULL
                ) AS valid
                """,
                {
                    "evidence_refs": Jsonb(payload.get("evidence_refs")),
                    "note": payload.get("note"),
                    "selection": Jsonb(pairwise_selection),
                    "payload": Jsonb(payload),
                },
            ).fetchone()
            assert bool(row["valid"]) is _python_persisted_form_accepts(BlindPairwiseSubmission, payload)
        for dataset_role in ("validation", "development"):
            owner_selection = _current_pairwise_selection(dataset_role=dataset_role)
            valid = conn.execute(
                "SELECT news_current_review_selection_valid(%s, 'pairwise') AS valid",
                (Jsonb(owner_selection),),
            ).fetchone()
            drift = conn.execute(
                "SELECT news_current_review_selection_valid(%s, 'pairwise') AS valid",
                (Jsonb(owner_selection | {"retired": True}),),
            ).fetchone()
            assert bool(valid["valid"]) is True
            assert bool(drift["valid"]) is False
    finally:
        conn.close()


def test_retained_json_validators_meet_native_insert_and_update_budget() -> None:
    oi, oi_metadata = _HISTORICAL_OI_SIGNAL, _HISTORICAL_OI_METADATA
    liquidation, liquidation_metadata = _HISTORICAL_LIQUIDATION_FACT, _HISTORICAL_LIQUIDATION_METADATA
    review = _current_review_payload(production_sized=True)
    selection = {
        "stratum": "random_control",
        "stratum_zh": "随机对照",
        "reason": "coverage_control",
        "reason_zh": "覆盖对照",
        "sampling_probability": 0.02,
        "selection_version": "news_review_sampler_v3",
    }
    conn = connect_postgres_test(read_only=False)
    try:
        with conn.transaction():
            conn.execute("SET LOCAL statement_timeout = '500ms'")
            conn.execute(
                """
                CREATE TEMP TABLE current_validator_budget (
                  id integer PRIMARY KEY,
                  oi jsonb NOT NULL CHECK (news_current_oi_signal_valid(oi)),
                  oi_metadata jsonb NOT NULL CHECK (news_current_oi_metadata_valid(oi_metadata, true)),
                  liquidation jsonb NOT NULL CHECK (news_current_liquidation_fact_valid(liquidation)),
                  liquidation_metadata jsonb NOT NULL
                    CHECK (news_current_liquidation_metadata_valid(liquidation_metadata, true)),
                  selection jsonb NOT NULL,
                  review jsonb NOT NULL CHECK (news_current_review_valid(
                    'judgment', 'event', 'news_review_v6', 'reader_contract_v2',
                    'event-current', 1, NULL, NULL,
                    review ->> 'should_push', review -> 'dimensions', review -> 'novelty',
                    review ->> 'first_bad_owner', review -> 'evidence_refs',
                    review ->> 'expected_correction', review ->> 'note', selection, review, NULL))
                ) ON COMMIT DROP
                """
            )
            conn.execute(
                """
                INSERT INTO current_validator_budget
                SELECT item, %(oi)s, %(oi_metadata)s, %(liquidation)s, %(liquidation_metadata)s,
                       %(selection)s, %(review)s
                  FROM generate_series(1, 250) AS item
                """,
                {
                    "oi": Jsonb(oi),
                    "oi_metadata": Jsonb(oi_metadata),
                    "liquidation": Jsonb(liquidation),
                    "liquidation_metadata": Jsonb(liquidation_metadata),
                    "selection": Jsonb(selection),
                    "review": Jsonb(review),
                },
            )
            conn.execute("UPDATE current_validator_budget SET review = review")
    finally:
        conn.close()
