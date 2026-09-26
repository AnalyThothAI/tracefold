from __future__ import annotations

import copy
from typing import Any, Final

import pytest
from psycopg.types.json import Jsonb
from pydantic import TypeAdapter, ValidationError

from tests.postgres_test_utils import connect_postgres_test
from tests.support.news_legacy import legacy_editorial, legacy_taxonomy
from tracefold.news.artifact_identity import canonical_sha
from tracefold.news.models import TriageVerdict
from tracefold.news.review.desk import _V8_NO_TAXONOMY, EventRubricSubmission

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
    dimensions = {"factual_fidelity": "pass"}
    expected: dict[str, Any] | None = None
    if production_sized:
        evidence_refs = [f"ref-{index:02d}-" + "x" * 493 for index in range(32)]
        note = "n" * 2_000
        dimensions |= {
            "direction": "fail",
            "asset_grounding": "fail",
            "fact_kind": "fail",
        }
        expected = {
            "direction": "bullish",
            "assets": [
                {"symbol": f"ASSET{index:02d}".ljust(32, "X"), "market_type": "equity", "role": "primary"}
                for index in range(16)
            ],
            "fact_kind": "official_measure",
        }
    return (
        EventRubricSubmission(
            should_push="should_hold",
            dimensions=dimensions,
            novelty={"judgment": "new_fact"},
            first_bad_owner="triage_prompt",
            evidence_refs=evidence_refs,
            expected=expected,
            note=note,
        ).model_dump(mode="json")
        | _V8_NO_TAXONOMY
    )


def _review_row_accepted_by_python(payload: dict[str, Any]) -> bool:
    """The ReviewDesk writes the submission plus the two constant `news_review_v8` taxonomy keys (#706)."""

    if any(key not in payload or payload[key] != value for key, value in _V8_NO_TAXONOMY.items()):
        return False
    submission = {key: value for key, value in payload.items() if key not in _V8_NO_TAXONOMY}
    return _python_persisted_form_accepts(EventRubricSubmission, submission)


def test_news_current_json_validators_match_the_python_contract() -> None:
    verdict = TriageVerdict(
        novelty="new_fact",
        restates=-1,
        assets=[{"symbol": f"ASSET{index}", "market_type": "spot", "role": "mentioned"} for index in range(8)],
        direction="bullish",
        scope="sector",
        fact_kind="official_measure",
        evidence_ref="c1",
        confidence=1,
        headline_zh="跨语言契约",
        why_zh="数据库必须拒绝绕过应用模型的同一无效当前事实。",
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
    conn = connect_postgres_test(read_only=False)
    try:
        for payload in verdict_corpus:
            db_accepts = bool(
                conn.execute("SELECT news_current_triage_verdict_valid(%s) AS valid", (Jsonb(payload),)).fetchone()[
                    "valid"
                ]
            )
            assert db_accepts is _python_persisted_form_accepts(TriageVerdict, payload)
    finally:
        conn.close()


def test_the_expected_asset_validator_admits_both_shapes_and_only_a_vocabulary_market() -> None:
    """#651 §6.2: a reviewer's stated answer carries the market, and history does not.

    `news_current_review_expected_valid` enumerated the asset keys exactly as `{symbol, role}`, so the
    first accepted review naming a typed asset would have been refused outright. Both shapes are
    admitted for the same reason every other key-set widening in this schema admits two: the reviews
    written before the cut are audit truth and are never rewritten. What is *not* admitted is a market
    outside the vocabulary — the whole point of typing the field is that it is a closed answer.
    """

    def _expected(assets: list[dict[str, object]]) -> dict[str, object]:
        return {
            "magnitude": None,
            "direction": None,
            "assets": assets,
            "trade_impact_breadth": None,
            "trade_tradability": None,
            "trade_surprise": None,
            "trade_development_delta": None,
            "trade_channels": None,
            "trade_affected_markets": None,
            "reader_value": None,
        }

    corpus = [
        # The shape every review written before #651 carries.
        (_expected([{"symbol": "SEI", "role": "primary"}]), True),
        (_expected([{"symbol": "SEI", "market_type": "equity", "role": "primary"}]), True),
        # `unknown` is a reviewer saying nothing was established, which is a value and not an absence.
        (_expected([{"symbol": "SEI", "market_type": "unknown", "role": "primary"}]), True),
        (_expected([{"symbol": "SEI", "market_type": "token", "role": "primary"}]), False),
        (_expected([{"symbol": "SEI", "market_type": None, "role": "primary"}]), False),
        (_expected([{"symbol": "SEI", "market_type": "equity", "role": "subject"}]), False),
        (_expected([{"symbol": "SEI", "market_type": "equity", "role": "primary", "retired": True}]), False),
    ]

    conn = connect_postgres_test(read_only=False)
    try:
        for payload, expected_valid in corpus:
            row = conn.execute("SELECT news_current_review_expected_valid(%s) AS valid", (Jsonb(payload),)).fetchone()
            assert bool(row["valid"]) is expected_valid, payload
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
    taxonomy_drift = review | {"taxonomy_review": review["taxonomy_review"] | {"retired": True}}
    stated_taxonomy = review | {"taxonomy": {"event_family": "market_access"}}
    review_corpus = [review, review | {"retired": True}, taxonomy_drift, stated_taxonomy]
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
                      'judgment', 'event', 'news_review_v8', 'reader_contract_v3',
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
            # A stated taxonomy is still a shape the v8 row contract admits; the desk can no longer write one.
            expected = _review_row_accepted_by_python(payload) or payload is stated_taxonomy
            assert bool(row["valid"]) is expected, (index, payload)
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
                    'judgment', 'event', 'news_review_v8', 'reader_contract_v3',
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


def test_the_editorial_validator_holds_every_written_shape_and_refuses_an_invented_one() -> None:
    """#651 §5.3 and #675 §1: what `news_verdicts.editorial` is allowed to be, stated at the database.

    Four things have to be true at once. A `news_editorial_v4` judgment whose taxonomy Predictor failed
    alone is accepted with a JSON-null taxonomy and a `news_program_*` code. A `news_editorial_v3`
    document -- which carries the seven relevance codes this cut deleted -- and a `news_editorial_v2`
    one -- which the Python contract could already no longer produce -- both keep validating, because
    every judgment written before their cut carries them, is audit truth, and is never rewritten. And
    nothing else does: a status that disagrees with the taxonomy beside it, an error code from another
    vocabulary, or a document wearing another version's number are all refused.
    """

    relevance = {
        "impact_breadth": "single_instrument",
        "tradability": "direct",
        "surprise": "unscheduled",
        "development_delta": "state_change",
        "channels": ["exchange_access"],
        "affected_markets": ["single_asset"],
        "reader_value": "realtime",
    }
    axes = legacy_taxonomy(event_family="market_access", change_state="effective")

    def sealed(payload: dict[str, Any]) -> dict[str, Any]:
        return payload | {"editorial_sha256": canonical_sha(payload)}

    available = legacy_editorial(source_authority="issuer_first_party", taxonomy=legacy_taxonomy()).document
    unavailable = legacy_editorial(
        source_authority="unknown",
        taxonomy_error_code="news_program_output_truncated",
    ).document
    historical_v3 = sealed(
        {key: value for key, value in available.items() if key != "editorial_sha256"}
        | {"editorial_contract_version": "news_editorial_v3", "relevance": relevance}
    )
    historical_v2 = sealed(
        {
            "editorial_contract_version": "news_editorial_v2",
            "editorial_origin": "model",
            "relevance": relevance,
            "taxonomy": axes | {"source_authority": "reputable_secondary"},
        }
    )
    accepted = [available, unavailable, historical_v3, historical_v2]
    refused = [
        # A status that disagrees with the taxonomy beside it, in both directions.
        sealed({**unavailable, "taxonomy_status": "available"} | {"taxonomy_error_code": None}),
        sealed({key: value for key, value in available.items() if key != "editorial_sha256"} | {"taxonomy": None}),
        # An error code from some other vocabulary, and an empty one.
        sealed(
            {key: value for key, value in unavailable.items() if key != "editorial_sha256"}
            | {"taxonomy_error_code": "boom"}
        ),
        sealed(
            {key: value for key, value in unavailable.items() if key != "editorial_sha256"}
            | {"taxonomy_error_code": "news_program_"}
        ),
        # The v2 body under the v3 version, and the v4 body under the v2 version.
        sealed(
            {key: value for key, value in historical_v2.items() if key != "editorial_sha256"}
            | {"editorial_contract_version": "news_editorial_v3"}
        ),
        sealed(
            {key: value for key, value in available.items() if key != "editorial_sha256"}
            | {"editorial_contract_version": "news_editorial_v2"}
        ),
        # The relevance block is what separates v3 from v4, in both directions.
        sealed(
            {key: value for key, value in available.items() if key != "editorial_sha256"}
            | {"editorial_contract_version": "news_editorial_v3"}
        ),
        sealed(
            {key: value for key, value in historical_v3.items() if key != "editorial_sha256"}
            | {"editorial_contract_version": "news_editorial_v4"}
        ),
        # A v2 taxonomy whose authority is outside the vocabulary.
        sealed(
            {key: value for key, value in historical_v2.items() if key != "editorial_sha256"}
            | {"taxonomy": axes | {"source_authority": "a blog"}}
        ),
        # A v4 authority outside the vocabulary.
        sealed(
            {key: value for key, value in available.items() if key != "editorial_sha256"}
            | {"source_authority": "a blog"}
        ),
        # The seal itself still has to hold.
        available | {"editorial_sha256": "0" * 64},
    ]

    conn = connect_postgres_test(read_only=False)
    try:
        for payload in accepted:
            row = conn.execute("SELECT news_current_model_editorial_valid(%s) AS valid", (Jsonb(payload),)).fetchone()
            assert bool(row["valid"]) is True, payload["editorial_contract_version"]
        for payload in refused:
            row = conn.execute("SELECT news_current_model_editorial_valid(%s) AS valid", (Jsonb(payload),)).fetchone()
            assert bool(row["valid"]) is False, payload
    finally:
        conn.close()


def test_the_review_taxonomy_validator_still_admits_both_historical_shapes() -> None:
    """Reviews accepted before #706 carry a taxonomy with or without `source_authority`; they are never
    rewritten, so the validator that guards them keeps admitting both and refusing anything else."""

    six = legacy_taxonomy(event_family="market_access", change_state="effective")
    conn = connect_postgres_test(read_only=False)
    try:

        def valid(payload: dict[str, Any]) -> bool:
            return bool(
                conn.execute("SELECT news_current_review_taxonomy_valid(%s) AS valid", (Jsonb(payload),)).fetchone()[
                    "valid"
                ]
            )

        assert valid(six) is True
        assert valid(six | {"source_authority": "regulatory_filing"}) is True
        assert valid(six | {"source_authority": "a blog"}) is False
        assert valid(six | {"retired": True}) is False
        assert valid({key: value for key, value in six.items() if key != "event_family"}) is False
    finally:
        conn.close()
