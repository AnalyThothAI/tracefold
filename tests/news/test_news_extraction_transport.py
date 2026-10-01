"""The extraction schema a constrained decoder is asked to follow, how a generated claim is repaired, and the
route an unusable answer takes.

Production 2026-09-29/30 (#742): the per-claim `TransportClaim | dict` envelope advertised an open object per
claim, so qwen's json_schema grammar stopped binding claims. 27 revisions failed as `news_claim_schema_invalid`
on the first answer, without the declared fallback. Replaying all 27 on the same model, every claim left the
typed branch (77/77 began with `slot`, never the schema's first key `topics`) and wrote its citations or topics
inside `fields`, a phase `not_applicable` or a mode `schedule`. The claim shapes below are those answers.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from dspy.lm15 import Message, Response, Usage  # type: ignore[import-untyped]

from tests.support.news_update_semantic import MemoryCache, TaskBackend, material
from tests.support.scripted_lm import ScriptedLM
from tracefold.news.adapters.extraction import DspyExtractor
from tracefold.news.artifact_identity import canonical_json
from tracefold.news.updates.contracts import Extraction, FrozenInput
from tracefold.news.updates.judgment import Budget, ContractFault, NewsJudgments
from tracefold.news.updates.semantics import SemanticAnalyzer
from tracefold.news.updates.topics import CODEBOOK

HEADLINE = "L3HARRIS $LHX RECEIVES $6B THAAD PROPULSION CONTRACT FROM LOCKHEED MARTIN $LMT"
TOPIC = next(iter(CODEBOOK))[0]
CRYPTO, BANKRUPTCY, MARKETS = "medtop:20001279", "medtop:20000174", "medtop:20000385"


def _source(text: str = HEADLINE) -> FrozenInput:
    return FrozenInput(event_id="event", revision=1, lineage_id="event", evidence=(material(text),))


def _claim(slot: str, **overrides: Any) -> dict[str, Any]:
    claim: dict[str, Any] = {
        "slot": slot,
        "statement": "L3Harris received a $6B THAAD propulsion contract from Lockheed Martin.",
        "fields": {"subject": "L3Harris", "action": "received", "object": "THAAD propulsion contract"},
        "citations": [{"evidence_ref": "e1", "quote": HEADLINE}],
        "topics": [TOPIC],
    }
    claim.update(overrides)
    return claim


def _answer(*claims: dict[str, Any]) -> dict[str, Any]:
    return {"result": {"claims": list(claims)}}


def _truncated(text: str) -> Response:
    return Response(
        id=None,
        model="scripted/primary",
        message=Message.assistant(text),
        finish_reason="length",
        usage=Usage(input_tokens=0, output_tokens=4000, total_tokens=4000, cache_read_tokens=0),
    )


def _extractor(*route: ScriptedLM) -> DspyExtractor:
    return DspyExtractor(lambda: route, model_identity="fixture", topics=dict(CODEBOOK))


def _analyzer(extractor: DspyExtractor) -> SemanticAnalyzer:
    return SemanticAnalyzer(extractor, NewsJudgments(generated=TaskBackend({}), cache=MemoryCache()), topics=())


def _extract(text: str, *claims: dict[str, Any]) -> Extraction:
    """One generated answer through extraction and grounding, as the semantic worker reads it."""

    route = ScriptedLM([_answer(*claims)], model="scripted/primary")
    return asyncio.run(_analyzer(_extractor(route)).extract(_source(text), Budget.start(5)))


def _response_schema(lm: ScriptedLM) -> dict[str, Any]:
    fmt = lm.requests[0].config.response_format
    assert fmt["type"] == "json_schema"
    schema: dict[str, Any] = json.loads(canonical_json(fmt["schema"]))
    return schema


def _open_objects(node: Any) -> list[Any]:
    if isinstance(node, dict):
        found = [node] if node.get("type") == "object" and node.get("additionalProperties") is not False else []
        return found + [row for value in node.values() for row in _open_objects(value)]
    if isinstance(node, list):
        return [row for value in node for row in _open_objects(value)]
    return []


def test_the_constrained_extraction_schema_binds_every_claim_while_parsing_stays_per_claim() -> None:
    primary = ScriptedLM([_answer(_claim("ok"), _claim("broken", citations=[]))], model="scripted/primary")
    value = asyncio.run(_extractor(primary).extract(_source()))
    schema = _response_schema(primary)
    envelope = schema["$defs"]["ExtractionEnvelope"]["properties"]
    assert envelope["claims"]["items"] == {"$ref": "#/$defs/TransportClaim"}
    assert schema["$defs"]["TransportClaim"]["properties"]["topics"]["items"] == {"type": "string"}
    # No open object anywhere: an open alternative lets a constrained decoder leave every enum and type.
    assert _open_objects(schema) == []
    # Parsing is still per claim: the claim without a citation is named, its sibling kept.
    assert [claim.slot for claim in value.claims] == ["ok"]
    assert [(row.slot, row.code) for row in value.discarded_claims] == [("broken", "news_claim_schema_invalid")]


# ---------------------------------------------------------------- repairs, one rule each, on replayed shapes


def test_citations_and_topics_written_inside_fields_are_put_back_on_their_claim(
    caplog: pytest.LogCaptureFixture,
) -> None:
    goldman = "Yesterday, Goldman Sachs chose Avalanche."
    poolin = "*BANKRUPT CRYPTO MINER POOLIN TO REDO DATA CENTER AUCTION: BLAW"
    value = _extract(
        f"{goldman}\n\n{poolin}",
        {
            "slot": "c1",
            "statement": "Goldman Sachs chose Avalanche.",
            "fields": {
                "subject": "Goldman Sachs",
                "action": "chose Avalanche",
                "mode": "observation",
                "phase": "completed",
                "assets": [{"symbol": "AVAX", "market_type": "crypto", "role": "primary"}],
                "citations": [{"evidence_ref": "e1", "quote": goldman}],
            },
            "topics": [CRYPTO],
        },
        {
            "slot": "claim_1",
            "statement": "Bankrupt crypto miner Poolin is to redo its data center auction.",
            "fields": {
                "subject": "Poolin",
                "action": "redo data center auction",
                "phase": "announced",
                "topics": [BANKRUPTCY, CRYPTO],
            },
            "citations": [{"evidence_ref": "e1", "quote": poolin}],
        },
    )
    assert value.discarded_claims == ()
    assert [(claim.slot, claim.citations[0].quote, claim.topics) for claim in value.claims] == [
        ("c1", goldman, (CRYPTO,)),
        ("claim_1", poolin, (BANKRUPTCY, CRYPTO)),
    ]
    assert "news_extraction_claim_repaired index=0 errors=[('fields.citations', 'misplaced')]" in caplog.text
    assert goldman not in caplog.text


def test_a_reading_outside_its_options_is_unknown_and_a_not_applicable_phase_is_no_phase() -> None:
    headline = "Asia Fuel Oil-HSFO premium extends slide, Middle East supply improves"
    fields = {"subject": "Asia Fuel Oil-HSFO premium", "action": "extends slide", "mode": "observation"}
    value = _extract(
        headline,
        {
            "slot": "c1",
            "statement": "Asia Fuel Oil-HSFO premium extends slide",
            "fields": {
                **fields,
                "phase": "not_applicable",
                "assets": [{"symbol": "Fuel Oil-HSFO", "market_type": "bond", "role": "primary"}],
            },
            "citations": [{"evidence_ref": "e1", "quote": "Asia Fuel Oil-HSFO premium extends slide"}],
        },
        {
            "slot": "c2",
            "statement": "Middle East supply improves",
            "fields": {**fields, "subject": "Middle East supply", "mode": "announcement", "content_kind": "news"},
            "citations": [{"evidence_ref": "e1", "quote": "Middle East supply improves"}],
        },
    )
    first, second = (claim.fields for claim in value.claims)
    assert first.phase is None
    assert [(asset.symbol, asset.market_type) for asset in first.assets] == [("Fuel Oil-HSFO", "unknown")]
    assert (second.mode, second.content_kind) == ("unknown", "other")


def test_a_null_optional_field_a_stray_key_and_a_missing_slot_are_repaired() -> None:
    claim = _claim("ignored", fields={"subject": "L3Harris", "action": "received", "object": None, "conditions": None})
    del claim["slot"]
    claim["confidence"] = 0.9
    claim["fields"]["time"] = "today"
    [kept] = _extract(HEADLINE, claim).claims
    assert (kept.slot, kept.fields.object, kept.fields.conditions) == ("#0", "", ())


def test_an_unparseable_entry_is_left_out_alone() -> None:
    fields = {
        "subject": "L3Harris",
        "action": "received",
        "conditions": ["subject to approval", None],
        "quantities": [
            {"name": "share", "value": "2/3", "unit": "ratio"},
            {"name": "value", "value": "6", "unit": "B"},
        ],
        "assets": [{"symbol": "LHX", "market_type": "equity", "role": "issuer"}],
    }
    citations = [{"evidence_ref": "e1"}, {"evidence_ref": "e1", "quote": HEADLINE}]
    [kept] = _extract(HEADLINE, _claim("a", fields=fields, citations=citations)).claims
    assert kept.fields.conditions == ("subject to approval",)
    assert [(row.name, row.value) for row in kept.fields.quantities] == [("value", "6")]
    assert kept.fields.assets == ()
    assert [citation.quote for citation in kept.citations] == [HEADLINE]


def test_a_topic_is_kept_by_the_code_it_names_and_only_an_unknown_topic_is_dropped() -> None:
    # A topic written as an object was seen on this route before (`{"code": "medtop:20000344"}`).
    label = dict(CODEBOOK)[MARKETS]
    topics = [{"code": "medtop:20000344"}, label, "not-a-topic", {"code": "medtop:20000344"}, CRYPTO, TOPIC]
    [kept] = _extract(HEADLINE, _claim("a", topics=topics)).claims
    assert kept.topics == tuple(sorted(("medtop:20000344", MARKETS, CRYPTO)))


def test_a_quote_wrapped_in_emphasis_marks_is_grounded_to_the_source_text() -> None:
    # The fixed-schema replay: the headline carries a leading asterisk, the model closed it with another.
    headline = "*BANKRUPT CRYPTO MINER POOLIN TO REDO DATA CENTER AUCTION: BLAW"
    claim = _claim("a", citations=[{"evidence_ref": "e1", "quote": f"{headline}*"}])
    [kept] = _extract(headline, claim).claims
    # The stored quote is the source's own text between the marks.
    assert kept.citations[0].quote == headline.removeprefix("*")


def test_only_a_claim_without_a_statement_citation_subject_or_action_is_discarded() -> None:
    # The truncated tail of the replayed digest (`fields: {}`, no citations) next to its complete siblings.
    tail = {"slot": "claim_13", "statement": "美联储古尔斯比表示", "fields": {}}
    value = _extract(HEADLINE, _claim("a"), tail, _claim("b", statement=""), _claim("c", fields={"subject": "L3"}))
    assert [claim.slot for claim in value.claims] == ["a"]
    assert [row.slot for row in value.discarded_claims] == ["claim_13", "b", "c"]


def test_a_reused_slot_keeps_a_distinct_claim_and_drops_only_a_restatement() -> None:
    distinct = _claim("a", statement="Lockheed Martin awarded L3Harris a THAAD propulsion contract.")
    value = _extract(HEADLINE, _claim("a"), distinct, _claim("a"))
    assert [claim.slot for claim in value.claims] == ["a", "#1"]
    assert [(row.slot, row.code) for row in value.discarded_claims] == [("a", "news_duplicate_claim_slot")]


# ---------------------------------------------------------------- the declared fallback


def test_an_answer_with_no_usable_claim_asks_the_declared_fallback() -> None:
    primary = ScriptedLM([_answer(_claim("a", fields={"subject": "L3Harris"}))], model="scripted/primary")
    fallback = ScriptedLM([_answer(_claim("a"))], model="scripted/fallback", structured_output="json_object")
    value = asyncio.run(_analyzer(_extractor(primary, fallback)).extract(_source(), Budget.start(5)))
    assert [claim.slot for claim in value.claims] == ["a"] and value.discarded_claims == ()
    assert len(primary.requests) == len(fallback.requests) == 1


def test_an_answer_cut_at_the_token_ceiling_asks_the_declared_fallback() -> None:
    complete = canonical_json(_answer(_claim("a"), _claim("b")))
    cut = complete[: complete.index('"slot":"b"') + 20]  # the repaired JSON would parse with a partial "b"
    primary = ScriptedLM([_truncated(cut)], model="scripted/primary")
    fallback = ScriptedLM([complete], model="scripted/fallback", structured_output="json_object")
    value = asyncio.run(_extractor(primary, fallback).extract(_source()))
    assert [claim.slot for claim in value.claims] == ["a", "b"] and value.discarded_claims == ()
    assert len(fallback.requests) == 1


def test_an_unusable_answer_on_the_last_route_fails_once_and_names_the_field(
    caplog: pytest.LogCaptureFixture,
) -> None:
    primary = ScriptedLM([_answer(_claim("a", citations=[]))], model="scripted/primary")
    with pytest.raises(ContractFault, match="news_claim_schema_invalid"):
        asyncio.run(_analyzer(_extractor(primary)).extract(_source(), Budget.start(5)))
    assert len(primary.requests) == 1
    # The formatted message names the location and error type; the generated text never reaches the log.
    assert "news_extraction_claim_schema_invalid index=0 errors=[('citations', 'too_short')]" in caplog.text
    assert HEADLINE not in caplog.text
