"""Original model-visible evidence stays bounded and keeps the qualifiers needed for faithful copy."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tracefold.news.artifact_identity import canonical_sha
from tracefold.news.learning.card_lint import lint_reader_card
from tracefold.news.program.artifact import render_model_evidence_json
from tracefold.news.program.contracts import TriageContext
from tracefold.news.program.signatures import ReaderCard

_FIXTURE = Path(__file__).parents[1] / "fixtures/news/reader_card_fidelity_cases.json"
_DOCUMENT = json.loads(_FIXTURE.read_text(encoding="utf-8"))
_CASES = {case["name"]: case for case in _DOCUMENT["cases"]}


@pytest.mark.parametrize("name", tuple(_CASES))
def test_original_evidence_replays_without_history_or_delivery_fields(name: str) -> None:
    case = _CASES[name]
    archived = TriageContext.model_validate(case["context"])
    context = archived.adapt_archived_excerpt()
    rendered = render_model_evidence_json(context.reader_card_payload(), predictor="reader_card")
    assert rendered != case["reader_evidence_json"]  # explicit v11 study, not historical v10 replay
    assert context.prepared_evidence.missing == ("legacy_excerpt_only",)
    payload = json.loads(rendered.splitlines()[1])
    assert set(payload) == {"event", "gate", "current_evidence", "related_evidence"}
    assert context.evidence.raw_first_line in "\n".join(span["text"] for span in payload["current_evidence"])
    assert len(context.evidence.content) <= 600
    assert len(context.evidence.raw_first_line) <= 300
    assert "event_status" not in payload
    assert "provider_score" not in payload["event"]
    assert canonical_sha(_DOCUMENT["cases"]) == _DOCUMENT["cases_sha256"]


def test_third_party_qualifiers_and_title_only_boundaries_are_preserved() -> None:
    pons = TriageContext.model_validate(_CASES["pons"]["context"])
    assert "READY TO TWAP" in pons.evidence.content
    assert "most of the past week" in pons.evidence.content
    assert pons.evidence.source == "theunipcs"
    assert TriageContext.model_validate(_CASES["iren"]["context"]).evidence.content == ""
    bitget = TriageContext.model_validate(_CASES["bitget"]["context"])
    assert "Bitget" not in bitget.evidence.title
    assert bitget.evidence.raw_first_line == "Bitget Announcement:"
    assert "Bitget Announcement:" in _CASES["bitget"]["reader_evidence_json"]


def test_evidence_builder_preserves_qualifiers_inside_its_bounds() -> None:
    context = TriageContext.from_card(
        {
            "event_id": "bounded",
            "leader_title": "Factory entry is conditional on the grid study" + "x" * 700,
            "leader_description": "Approval is pending; the advertised rate is 12% APR. " + "x" * 700,
            "raw_first_line": "Third-party report: " + "x" * 400,
        },
        watchlist=(),
        told_rows=(),
        now_ms=1,
        queue_lag_ms=0,
    )
    payload = context.reader_card_payload()
    assert len(context.evidence.title) == len(context.evidence.content) == 600
    assert len(context.evidence.raw_first_line) == 300
    assert "Approval is pending" in json.dumps(payload)
    assert "12% APR" in json.dumps(payload)


@pytest.mark.parametrize("why", ["尚待批准", "获批后产量有望增加", "公司称或将提高产量"])
def test_short_faithful_or_conditional_copy_does_not_require_padding(why: str) -> None:
    card = ReaderCard(headline_zh="工厂待批", why_zh=why)
    lint = lint_reader_card(**card.model_dump(exclude={"source_refs"}), source_title="Factory approval pending")
    assert lint.gate == ""
    assert dict(lint.outcomes)["headline_length"] == "lint_pass"
    assert dict(lint.outcomes)["banned_filler"] == "lint_pass"


@pytest.mark.parametrize("case", _DOCUMENT["semantic_diagnostics"], ids=lambda case: case["name"])
def test_semantic_diagnostics_are_not_claimed_as_structural_validation(case):
    from tracefold.news.evidence import assemble_evidence, query_for

    card = {"leader_title": case["source"]}
    item = {"item_id": case["name"], "evidence_text": case["source"], "provider_params_available_at_ms": 1}
    prepared = assemble_evidence(card, item, query=query_for(card, item, cutoff=2), candidates=[])
    context = TriageContext.from_card(
        card, watchlist=(), told_rows=(), now_ms=2, queue_lag_ms=0, prepared_evidence=prepared
    )
    output = ReaderCard(source_refs=("c1",), headline_zh=case["headline_zh"], why_zh=case["why_zh"])
    # A structurally valid, visible c1 can still contradict its source. These cases
    # belong to semantic evaluation; they must not create a regex-based refusal gate.
    assert output.source_refs[0] in {s.ref_id for s in prepared.current_evidence}
    assert context.reader_card_payload()["current_evidence"][0]["text"] == case["source"]
    assert case["expected_boundary"]
