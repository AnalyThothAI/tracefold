"""Generated transport boundaries and single-owner semantic clarification."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

from tests.news.test_news_event_update_judgments import MemoryCache
from tests.news.test_news_event_update_notifications import TaskBackend
from tests.news.test_news_event_updates_core import draft, material, prior_of, update_one
from tracefold.news.updates import dspy_backend
from tracefold.news.updates.contracts import Extraction, FrozenInput
from tracefold.news.updates.dspy_backend import DspyCardComposer, DspyExtractor, GeneratedJudgments
from tracefold.news.updates.judgment import Budget, ContractFault, NewsJudgments, Question
from tracefold.news.updates.notification import NotificationPlanner, ReaderSnapshot
from tracefold.news.updates.public import public_updates
from tracefold.news.updates.semantics import SemanticAnalyzer, assemble_update

STAMP = 1_790_405_000_000


def generated(monkeypatch: pytest.MonkeyPatch, reply: Any) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def answer(signature: Any, route: Any, **inputs: Any) -> Any:
        calls.append(inputs)
        value = reply(inputs) if callable(reply) else reply
        # Apply the same result type used by DSPy's JSON adapter.
        return SimpleNamespace(result=signature.output_fields["result"].annotation.model_validate(value))

    monkeypatch.setattr(dspy_backend, "_generate", answer)
    return calls


def extraction_source() -> tuple[FrozenInput, dict[str, Any]]:
    _source, _extraction, head = update_one()
    evidence = material("The governor says five people were injured.", revision=2)
    source = FrozenInput(
        event_id=head.event_id, revision=2, lineage_id="later", evidence=(evidence,), prior=prior_of(head)
    )
    claim = draft(evidence).model_dump(mode="json")
    claim["citations"][0]["evidence_ref"] = "e1"
    return source, {"claims": [claim]}


@pytest.mark.parametrize(
    "bad_relation",
    [
        {"slot": "a", "previous_ref": "p999", "relation": "equivalent"},
        {"slot": "absent", "previous_ref": "p1", "relation": "equivalent"},
        {"slot": "a", "previous_ref": "p1", "relation": "not_an_option"},
    ],
)
def test_bad_optional_hint_preserves_core_and_existing_understanding_fills_it(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    bad_relation: dict[str, Any],
) -> None:
    source, reply = extraction_source()
    reply.update(relations=[bad_relation], supports=[{"slot": "a", "evidence_ref": "e1", "relation": "reports"}])
    calls = generated(monkeypatch, reply)
    backend = TaskBackend({"relation": "unrelated"})
    analyzer = SemanticAnalyzer(
        extractor=DspyExtractor(lambda: None, model_identity="fixture", topics={}),
        judgments=NewsJudgments(generated=backend, cache=MemoryCache()),
        topics=(),
    )

    async def run() -> None:
        extracted = await analyzer.extract(source, Budget.start(5))
        assert len(extracted.claims) == 1 and extracted.relations == ()
        assert extracted.supports[0].evidence_ref == source.evidence[0].ref
        understood = await analyzer.understand(source, extracted, Budget.start(5))
        assert understood.relations[0].previous_ref == source.prior[0].claim.ref
        assert understood.relations[0].relation == "unrelated"

    asyncio.run(run())
    sent = json.loads(calls[0]["evidence_json"])
    assert sent["evidence"][0]["ref"] == "e1"
    assert sent["prior"][0]["claim"]["ref"] == "p1"
    assert sent["evidence"][0]["text"] == source.evidence[0].text
    assert "news_extraction_hint_discarded" in caplog.text
    assert [call[0] for call in backend.calls] == ["relation"]


def test_conflicting_duplicate_support_hints_are_recomputed_not_last_answer_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, reply = extraction_source()
    reply["supports"] = [
        {"slot": "a", "evidence_ref": "e1", "relation": value} for value in ("supports", "refutes", "supports")
    ]
    generated(monkeypatch, reply)
    value = asyncio.run(
        DspyExtractor(lambda: None, model_identity="fixture", topics={}).extract(source, extract_only=False)
    )
    assert len(value.claims) == 1 and value.supports == ()


@pytest.mark.parametrize(
    "citation",
    [
        {"evidence_ref": "e999", "quote": "The governor says five people were injured."},
        {"evidence_ref": "e1", "quote": "Ten people were injured."},
    ],
)
def test_bad_core_citation_is_still_rejected(monkeypatch: pytest.MonkeyPatch, citation: dict[str, str]) -> None:
    source, reply = extraction_source()
    reply["claims"][0]["citations"] = [citation]
    generated(monkeypatch, reply)
    analyzer = SemanticAnalyzer(
        extractor=DspyExtractor(lambda: None, model_identity="fixture", topics={}),
        judgments=NewsJudgments(generated=TaskBackend({}), cache=MemoryCache()),
        topics=(),
    )
    with pytest.raises(ContractFault, match="news_citation_not_in_frozen_source"):
        asyncio.run(analyzer.extract(source, Budget.start(5)))


def test_link_only_empty_extraction_adopts_without_public_delta_or_card(monkeypatch: pytest.MonkeyPatch) -> None:
    evidence = material("Read full report here: https://example.org/report")
    source = FrozenInput(event_id="link", revision=1, lineage_id="link", evidence=(evidence,))
    generated(monkeypatch, {"claims": []})
    value = asyncio.run(
        DspyExtractor(lambda: None, model_identity="fixture", topics={}).extract(source, extract_only=False)
    )
    head = assemble_update(source, value, None, adopted_at_ms=STAMP + 10)
    assert head is not None and head.claims == () and head.open_questions == ()
    assert public_updates(head, semantic_completed_at_ms=STAMP + 5) == ()
    planner = NotificationPlanner(NewsJudgments(generated=TaskBackend({}), cache=MemoryCache()))
    plan = asyncio.run(
        planner.plan(
            head, ReaderSnapshot(channel="news", revision="test", receipts=()), Budget.start(5), now_ms=STAMP + 20
        )
    )
    assert plan.action == "no_notification"


@pytest.mark.parametrize("answer", ["decision", "unknown", None])
@pytest.mark.parametrize("use_native", [False, True])
def test_mode_is_clarified_and_cached_before_adoption_never_in_notification(
    answer: str | None, use_native: bool
) -> None:
    evidence = material("Agency announces a tariff.")
    source = FrozenInput(event_id="mode", revision=1, lineage_id="mode", evidence=(evidence,))
    extracted = Extraction(claims=(draft(evidence, mode="unknown"),))
    backend = TaskBackend({"support": "reports", **({"mode": answer} if answer is not None else {})})
    native = (
        TaskBackend(
            {"mode": "unknown", "phase": "announced", "content_kind": "official_measure", "support": "reports"},
            identity="native",
        )
        if use_native
        else None
    )
    judgments = NewsJudgments(generated=backend, native=native, cache=MemoryCache())
    analyzer = SemanticAnalyzer(extractor=SimpleNamespace(identity="fixture"), judgments=judgments, topics=())

    async def run() -> None:
        first = await analyzer.understand(source, extracted, Budget.start(5))
        again = await analyzer.understand(source, extracted, Budget.start(5))
        assert first == again
        assert first.claims[0].fields.mode == (answer or "unknown")
        head = assemble_update(source, first, None, adopted_at_ms=STAMP + 1)
        assert head is not None
        count = len(backend.calls)
        plan = await NotificationPlanner(judgments).plan(
            head,
            ReaderSnapshot(channel="news", revision="test", receipts=()),
            Budget.start(5),
            now_ms=STAMP + 2,
        )
        assert plan.action == ("notify" if answer == "decision" else "no_notification")
        assert len(backend.calls) == count

    asyncio.run(run())
    assert [call[0] for call in backend.calls].count("mode") == (2 if answer is None else 1)


def test_card_aliases_decode_and_only_cited_provenance_is_passed(monkeypatch: pytest.MonkeyPatch) -> None:
    source, _extracted, head = update_one()
    calls = generated(
        monkeypatch,
        {"headline_zh": "机构宣布关税", "lines": [{"claim_ref": "c1", "text_zh": "机构宣布关税，下月生效。"}]},
    )
    copy = asyncio.run(
        DspyCardComposer(lambda: None).compose(
            head.claims,
            sources={source.evidence[0].ref: source.evidence[0].source},
        )
    )
    assert copy.lines[0].claim_ref == head.claims[0].ref
    sent = json.loads(calls[0]["selected_claims_json"])
    assert sent[0]["claim_ref"] == "c1"
    assert sent[0]["citations"][0]["quote"] == source.evidence[0].text
    assert sent[0]["citations"][0]["source"]["publisher_id"] == "wire"


def test_card_cannot_name_an_unselected_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    _, _, head = update_one()
    generated(monkeypatch, {"headline_zh": "标题", "lines": [{"claim_ref": "c9", "text_zh": "正文"}]})
    with pytest.raises(ContractFault, match="news_card_claim_reference_unknown"):
        asyncio.run(DspyCardComposer(lambda: None).compose(head.claims, sources={}))


def test_generated_judgment_maps_local_answers_to_original_cache_id(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = generated(monkeypatch, {"answers": [{"item_id": "q1", "value": "full"}]})
    result = asyncio.run(
        GeneratedJudgments(lambda: None, model_identity="fixture").judge(
            "coverage",
            (Question(item_id="coverage:stable-hash", payload_json='{"claim": "A"}'),),
            context_json=None,
        )
    )
    assert calls[0]["items"][0]["item_id"] == "q1"
    assert result.answers[0].item_id == "coverage:stable-hash"
