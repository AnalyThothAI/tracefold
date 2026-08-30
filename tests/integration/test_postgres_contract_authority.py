from __future__ import annotations

import copy
from typing import Any

import pytest
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from tests.postgres_test_utils import connect_postgres_test
from tests.support.news_judgment import news_taxonomy
from tracefold.news.artifact_identity import canonical_sha
from tracefold.news.models import TriageVerdict
from tracefold.news.program.contracts import EditorialEnvelope, TradeRelevanceV1

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("postgres_clone_dsn")]


def _python_persisted_form_accepts(model: Any, payload: dict[str, Any]) -> bool:
    """Match the exact JSON shape the application writes after Pydantic materialization."""

    try:
        materialized = model.model_validate(payload).model_dump(mode="json")
    except ValidationError:
        return False
    return materialized == payload


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
