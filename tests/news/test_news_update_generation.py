"""Generated transport boundaries and single-owner semantic clarification."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

from tests.support.news_extraction_809 import boundary_case, extraction_case
from tests.support.news_reader import PushAll
from tests.support.news_update_semantic import (
    MemoryCache,
    TaskBackend,
    draft,
    generated,
    material,
    prior_of,
    update_one,
)
from tracefold.news.adapters.card_copy import CARD_INSTRUCTION, DspyCardComposer
from tracefold.news.adapters.extraction import DspyExtractor
from tracefold.news.adapters.semantic_judgments import GeneratedJudgments
from tracefold.news.notifications.contracts import ReaderRepairContext, ReaderSnapshot
from tracefold.news.notifications.planner import NotificationPlanner
from tracefold.news.updates.assembly import assemble_update
from tracefold.news.updates.contracts import (
    Citation,
    Claim,
    ClaimFields,
    Extraction,
    FrozenInput,
    OpenQuestion,
    Quantity,
    Source,
)
from tracefold.news.updates.judgment import Budget, ContractFault, NewsJudgments, Question
from tracefold.news.updates.semantics import SemanticAnalyzer

pytestmark = pytest.mark.usefixtures("synthetic_reader_calibration")

STAMP = 1_790_405_000_000


def extraction_source() -> tuple[FrozenInput, dict[str, Any]]:
    _source, _extraction, head = update_one()
    evidence = material("The governor says five people were injured.", revision=2)
    source = FrozenInput(
        event_id=head.event_id, revision=2, lineage_id="later", evidence=(evidence,), prior=prior_of(head)
    )
    claim = draft(evidence).model_dump(mode="json")
    claim["citations"][0]["evidence_ref"] = "e1"
    return source, {"claims": [claim]}


@pytest.mark.parametrize("case_id", [f"R{i}" for i in range(1, 8)])
def test_809_manual_complete_claims_survive_one_extraction_and_exact_grounding(monkeypatch, case_id) -> None:
    # Manual replies prove the transport, not that the real model follows the instruction.
    source, manual = extraction_case(case_id)
    reply = manual.model_dump(mode="json")
    for quote in reply["claims"][0]["citations"]:
        quote["evidence_ref"] = "e1"
    calls = generated(monkeypatch, reply)
    analyzer = _analyzer()
    result = asyncio.run(analyzer.extract(source, Budget.start(5)))
    assert result.claims == manual.claims
    assert len(calls) == 1
    assert json.loads(calls[0]["evidence_json"])["evidence"][0]["segments"][0]["text"] == source.evidence[0].text
    assert all(c.quote in source.evidence[0].text for c in result.claims[0].citations)
    if case_id == "R6":
        assert result.claims[0].fields.actor_role == "unknown"
        assert "SpaceX" not in result.claims[0].statement


@pytest.mark.parametrize("case_id", ["B1", "B2", "B3"])
def test_809_different_project_conditional_month_and_unknown_actor_preserve_source(monkeypatch, case_id):
    source, manual = boundary_case(case_id)
    reply = manual.model_dump(mode="json")
    reply["claims"][0]["citations"][0]["evidence_ref"] = "e1"
    calls = generated(monkeypatch, reply)
    result = asyncio.run(_analyzer().extract(source, Budget.start(5)))
    assert result.claims == manual.claims and len(calls) == 1
    fields = result.claims[0].fields
    if case_id == "B1":
        assert fields.subject == "Project Cedar" and "Blast" not in result.claims[0].statement
    elif case_id == "B2":
        assert fields.effective_at == "October" and fields.phase == "announced"
        assert fields.conditions == ("unless the exports stop", "may")
    else:
        assert fields.subject == "unidentified attackers" and fields.actor_role == "unknown"


@pytest.mark.parametrize(
    "volunteered",
    [
        {"slot": "a", "previous_ref": "p1", "relation": "conflicts", "change_kind": "conflict"},
        {"slot": "a", "previous_ref": "p999", "relation": "equivalent"},
        {"slot": "a", "previous_ref": "p1", "relation": "not_an_option"},
    ],
)
def test_a_relation_the_extractor_volunteers_never_bypasses_the_relation_judge(
    monkeypatch: pytest.MonkeyPatch, volunteered: dict[str, Any]
) -> None:
    # #742 S2: extraction no longer outputs relations. A model that still writes one neither fails the
    # answer nor settles the comparison: the relation judge owns every relation.
    source, reply = extraction_source()
    reply.update(relations=[volunteered], supports=[{"slot": "a", "evidence_ref": "e1", "relation": "reports"}])
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
    # The Event's own current claim is extraction context; related Events' claims are not.
    assert sent["prior"][0]["claim"]["ref"] == "p1"
    assert sent["evidence"][0]["segments"][0]["text"] == source.evidence[0].text
    assert [call[0] for call in backend.calls] == ["relation"]


def test_conflicting_duplicate_support_hints_are_recomputed_not_last_answer_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, reply = extraction_source()
    reply["supports"] = [
        {"slot": "a", "evidence_ref": "e1", "relation": value} for value in ("supports", "refutes", "supports")
    ]
    generated(monkeypatch, reply)
    value = asyncio.run(DspyExtractor(lambda: None, model_identity="fixture", topics={}).extract(source))
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


def test_invalid_optional_topic_is_local_to_one_valid_core_claim(monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    source, reply = extraction_source()
    reply["claims"][0]["topics"] = ["outside-codebook"]
    generated(monkeypatch, reply)
    value = asyncio.run(DspyExtractor(lambda: None, model_identity="fixture", topics={}).extract(source))
    assert len(value.claims) == 1 and value.claims[0].topics == ()
    assert "news_optional_topic_discarded" in caplog.text


def test_a_first_read_without_claims_is_not_adopted(monkeypatch: pytest.MonkeyPatch) -> None:
    # #742 W1: an empty first version is no Event version: no head, no notification work, no public row.
    evidence = material("Read full report here: https://example.org/report")
    source = FrozenInput(event_id="link", revision=1, lineage_id="link", evidence=(evidence,))
    generated(monkeypatch, {"claims": []})
    value = asyncio.run(DspyExtractor(lambda: None, model_identity="fixture", topics={}).extract(source))
    assert value.claims == () and value.discarded_claims == ()
    assert assemble_update(source, value, None, adopted_at_ms=STAMP + 10) is None


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
        plan = await NotificationPlanner(PushAll(), MemoryCache()).plan(
            head,
            ReaderSnapshot(channel="news", revision="test", receipts=()),
            Budget.start(5),
            now_ms=STAMP + 2,
        )
        assert plan.action == "notify"
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
        DspyCardComposer(lambda: None, model_identity="test").compose(
            head.claims,
            sources={source.evidence[0].ref: source.evidence[0].source},
        )
    )
    assert copy.lines[0].claim_ref == head.claims[0].ref
    sent = json.loads(calls[0]["selected_claims_json"])
    assert sent[0]["claim_ref"] == "c1"
    assert sent[0]["citations"][0]["quote"] == source.evidence[0].text
    assert sent[0]["citations"][0]["source"]["publisher_id"] == "wire"
    assert set(sent[0]) == {"claim_ref", "statement", "fields", "citations"}
    assert set(sent[0]["citations"][0]["source"]) == {"publisher_id", "attribution", "origin_id"}


def test_an_increment_or_correction_card_is_written_against_the_earlier_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#742 item 13: the composer gets the text the reader already has and is told how to use it."""

    source, _extracted, head = update_one()
    calls = generated(
        monkeypatch,
        {"headline_zh": "补充：关税下月生效", "lines": [{"claim_ref": "c1", "text_zh": "补充：关税将于下月生效。"}]},
    )
    earlier = ReaderRepairContext(render="increment", intent_id="intent:earlier", body="机构宣布加征关税")
    asyncio.run(
        DspyCardComposer(lambda: None, model_identity="test").compose(
            head.claims,
            sources={source.evidence[0].ref: source.evidence[0].source},
            earlier={head.claims[0].ref: earlier},
        )
    )
    sent = json.loads(calls[0]["selected_claims_json"])
    assert sent[0]["earlier"] == {"render": "increment", "delivered_text": "机构宣布加征关税"}
    assert "补充：" in CARD_INSTRUCTION and "更正：" in CARD_INSTRUCTION
    # #742 PR-4: P005 copied P004's wrong term (支农再贷款 for PSL) when told to name the earlier fact. The
    # earlier text is only what not to repeat; every name, term and number comes from the claim.
    instruction = " ".join(CARD_INSTRUCTION.split())
    assert "naming the earlier fact" not in instruction
    assert "only shows what not to repeat" in instruction
    assert "take every name, term and number from the claim itself" in instruction


def test_bug_c_frozen_claim_gives_composer_the_exact_drone_and_capture_fact(monkeypatch: pytest.MonkeyPatch) -> None:
    # Frozen from the sent 2026-09-27 Persian Fars claim that was rendered as
    # a "second sunk US submarine" despite saying "second captured US drone".
    quote = "تصاویر دومین زهپاد شکارشدهٔ ارتش تروریستی آمریکا در تنگهٔ هرمز"
    claim = Claim(
        ref="cl:bug-c",
        statement="فارس اعلام کرد که تصاویر دومین زهپاد شکارشدهٔ ارتش تروریستی آمریکا در تنگهٔ هرمز منتشر شده است.",
        fields=ClaimFields(
            subject="خبرگزاری فارس",
            action="اعلام انتشار تصاویر دومین زهپاد شکارشدهٔ ارتش تروریستی آمریکا در تنگهٔ هرمز",
            object=quote,
            mode="observation",
            phase="completed",
            polarity="affirmative",
            quantities=(Quantity(name="نوبت زهپاد شکارشده", value="2", unit="فروند"),),
            content_kind="state_change",
        ),
        citations=(Citation(evidence_ref="ev:bug-c", quote=quote),),
        first_available_at_ms=STAMP,
    )
    calls = generated(
        monkeypatch,
        {
            "headline_zh": "法尔斯通讯社称发布第二架被捕获美军无人机的照片",
            "lines": [
                {"claim_ref": "c1", "text_zh": "法尔斯通讯社称，已发布在霍尔木兹海峡被捕获的第二架美军无人机照片。"}
            ],
        },
    )
    result = asyncio.run(
        DspyCardComposer(lambda: None, model_identity="test").compose(
            (claim,),
            sources={
                "ev:bug-c": Source(
                    publisher_id="fars", artifact_id="report", artifact_revision="1", first_available_at_ms=STAMP
                )
            },
        )
    )
    selected = json.loads(calls[0]["selected_claims_json"])
    assert selected[0]["citations"][0]["quote"] == quote
    assert selected[0]["fields"]["object"] == quote
    assert selected[0]["fields"]["quantities"][0]["value"] == "2"
    assert result.lines[0].claim_ref == claim.ref
    assert "无人机" in result.lines[0].text_zh and "潜艇" not in result.lines[0].text_zh


def test_card_cannot_name_an_unselected_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    _, _, head = update_one()
    generated(monkeypatch, {"headline_zh": "标题", "lines": [{"claim_ref": "c9", "text_zh": "正文"}]})
    with pytest.raises(ContractFault, match="news_card_claim_reference_unknown"):
        asyncio.run(DspyCardComposer(lambda: None, model_identity="test").compose(head.claims, sources={}))


def test_generated_judgment_maps_local_answers_to_original_cache_id(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = generated(monkeypatch, {"answers": [{"item_id": "q1", "value": "supports"}]})
    result = asyncio.run(
        GeneratedJudgments(lambda: None, model_identity="fixture").judge(
            "support",
            (Question(item_id="support:stable-hash", payload_json='{"claim": "A"}'),),
            context_json=None,
        )
    )
    assert calls[0]["items"][0]["item_id"] == "q1"
    assert result.answers[0].item_id == "support:stable-hash"


@pytest.mark.parametrize("unknown_ref", ["p1", "t1", "q99"])
@pytest.mark.parametrize("with_claims", [False, True])
def test_unknown_question_resolution_cannot_close_a_real_gap_or_discard_core(
    monkeypatch: pytest.MonkeyPatch, unknown_ref: str, with_claims: bool
) -> None:
    first, extracted, _head = update_one()
    extracted = extracted.model_copy(
        update={"open_questions": (OpenQuestion(question="Who implements it?", slots=("a",)),)}
    )
    head = assemble_update(first, extracted, None, adopted_at_ms=STAMP + 10)
    assert head is not None
    gap = head.open_questions[0]
    source = FrozenInput(
        event_id=head.event_id,
        revision=2,
        lineage_id="later",
        evidence=first.evidence,
        prior=prior_of(head),
        open_questions={gap.ref: gap},
    )
    claim = extracted.claims[0].model_dump(mode="json")
    claim["citations"][0]["evidence_ref"] = "e1"
    generated(
        monkeypatch,
        {
            "claims": [claim] if with_claims else [],
            "resolved_questions": [
                {"question_ref": unknown_ref, "citations": [{"evidence_ref": "e1", "quote": first.evidence[0].text}]}
            ],
        },
    )
    analyzer = SemanticAnalyzer(
        extractor=DspyExtractor(lambda: None, model_identity="fixture", topics={}),
        judgments=NewsJudgments(generated=TaskBackend({}), cache=MemoryCache()),
        topics=(),
    )
    value = asyncio.run(analyzer.extract(source, Budget.start(5)))
    assert len(value.claims) == int(with_claims)
    assert value.resolved_questions == ()
    adopted = assemble_update(source, value, head, adopted_at_ms=STAMP + 20) or head
    assert adopted.open_questions == (gap,)


@pytest.mark.parametrize("grounded", [False, True])
def test_supplied_question_resolution_still_requires_an_exact_current_citation(
    monkeypatch: pytest.MonkeyPatch, grounded: bool
) -> None:
    from tracefold.news.updates.contracts import KnowledgeGap

    source, reply = extraction_source()
    gap = KnowledgeGap(question="Who implements it?", claim_refs=(source.prior[0].claim.ref,))
    source = source.model_copy(update={"open_questions": {gap.ref: gap}})
    reply["resolved_questions"] = [
        {
            "question_ref": "q1",
            "citations": [{"evidence_ref": "e1", "quote": source.evidence[0].text if grounded else "Invented."}],
        }
    ]
    generated(monkeypatch, reply)
    analyzer = SemanticAnalyzer(
        extractor=DspyExtractor(lambda: None, model_identity="fixture", topics={}),
        judgments=NewsJudgments(generated=TaskBackend({}), cache=MemoryCache()),
        topics=(),
    )
    value = asyncio.run(analyzer.extract(source, Budget.start(5)))
    # An ungrounded resolution resolves nothing; it no longer fails the claims beside it.
    assert [row.question_ref for row in value.resolved_questions] == ([gap.ref] if grounded else [])
    assert len(value.claims) == 1


@pytest.mark.parametrize(
    ("field", "bad_detail"),
    [
        ("open_questions", {"question": "What changed?", "slots": ["t1", "t3"]}),
        ("open_questions", {"question": "What changed?", "slots": ["a"], "target_ref": "t999"}),
        ("open_questions", {"question": "What changed?", "slots": []}),
        (
            "implications",
            {"slots": ["t1"], "channel": "supply", "explanation": "Conditional.", "origin": "system_hypothesis"},
        ),
        ("implications", {"slots": ["a"], "channel": "supply", "explanation": "Conditional.", "origin": "invalid"}),
    ],
)
@pytest.mark.parametrize("with_claims", [False, True])
def test_invalid_optional_detail_cannot_fail_valid_or_empty_core(
    monkeypatch: pytest.MonkeyPatch, field: str, bad_detail: dict[str, Any], with_claims: bool
) -> None:
    source, reply = extraction_source()
    if not with_claims:
        reply["claims"] = []
    reply[field] = [bad_detail]
    generated(monkeypatch, reply)
    analyzer = SemanticAnalyzer(
        extractor=DspyExtractor(lambda: None, model_identity="fixture", topics={}),
        judgments=NewsJudgments(generated=TaskBackend({}), cache=MemoryCache()),
        topics=(),
    )
    value = asyncio.run(analyzer.extract(source, Budget.start(5)))
    assert len(value.claims) == int(with_claims)
    assert getattr(value, field) == ()


def test_grounded_optional_details_keep_their_claim_slots_and_read_target(monkeypatch: pytest.MonkeyPatch) -> None:
    from tracefold.news.updates.contracts import ReadTarget

    source, reply = extraction_source()
    target = ReadTarget(ref="news_item:stored", action="read_current_artifact", description="Stored statement")
    source = source.model_copy(update={"read_targets": (target,)})
    reply["open_questions"] = [{"question": "What changed?", "slots": ["a"], "target_ref": "t1"}]
    reply["implications"] = [
        {"slots": ["a"], "channel": "supply", "explanation": "If implemented.", "origin": "system_hypothesis"}
    ]
    generated(monkeypatch, reply)
    value = asyncio.run(DspyExtractor(lambda: None, model_identity="fixture", topics={}).extract(source))
    assert value.open_questions[0].target_ref == target.ref
    assert value.open_questions[0].slots == ("a",)
    assert value.implications[0].slots == ("a",)


# ---------------------------------------------------------------- #742 S1: one claim at a time


def _headline_source(text: str) -> FrozenInput:
    evidence = material(text)
    return FrozenInput(event_id="headline", revision=1, lineage_id="headline", evidence=(evidence,))


def _reply_claim(slot: str, statement: str, quote: str, **fields: Any) -> dict[str, Any]:
    return {
        "slot": slot,
        "statement": statement,
        "fields": {"subject": "Citigroup", "action": "partners", "mode": "decision", **fields},
        "citations": [{"evidence_ref": "e1", "quote": quote}],
    }


def _analyzer() -> SemanticAnalyzer:
    return SemanticAnalyzer(
        extractor=DspyExtractor(lambda: None, model_identity="fixture", topics={}),
        judgments=NewsJudgments(generated=TaskBackend({}), cache=MemoryCache()),
        topics=(),
    )


def test_a_title_cased_quote_of_an_all_caps_wsj_headline_is_kept_as_the_exact_source_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Production 2026-09-28: one Title-cased quote of this ALL-CAPS headline failed the whole extraction.
    headline = "CITIGROUP, COINBASE PACT TO ENABLE STABLECOIN PAYMENTS:  WSJ"
    source = _headline_source(headline)
    generated(
        monkeypatch,
        {
            "claims": [
                _reply_claim(
                    "pact", "Citigroup and Coinbase agree a stablecoin payments pact.", "Citigroup, Coinbase pact"
                ),
                _reply_claim("wsj", "WSJ reports the pact.", "stablecoin payments: WSJ"),
                _reply_claim("invented", "Coinbase shares jump 10%.", "Coinbase shares jump 10%"),
            ]
        },
    )
    value = asyncio.run(_analyzer().extract(source, Budget.start(5)))
    assert {claim.slot: claim.citations[0].quote for claim in value.claims} == {
        "pact": "CITIGROUP, COINBASE PACT",
        # Whitespace runs differ too; the stored quote is the source's own spelling.
        "wsj": "STABLECOIN PAYMENTS:  WSJ",
    }
    assert all(claim.citations[0].quote in headline for claim in value.claims)
    assert [(row.slot, row.code) for row in value.discarded_claims] == [
        ("invented", "news_citation_not_in_frozen_source")
    ]


def test_an_unparseable_reading_is_dropped_from_its_claim_and_a_broken_claim_only_from_the_answer(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Production 2026-09-28: `quantity "300亿"` failed the "中美 300 亿美元对等降税框架" Event as schema_invalid.
    text = "中美双方就300亿美元对等降税框架达成共识，对等提供降低关税待遇。"
    source = _headline_source(text)
    framework = _reply_claim(
        "framework",
        "中美就300亿美元对等降税框架达成共识。",
        "中美双方就300亿美元对等降税框架达成共识",
        phase="agreed",
        quantities=[
            {"name": "进口规模", "value": "300亿", "unit": "美元"},
            {"name": "双方", "value": "2", "unit": "方"},
        ],
        assets=[{"symbol": "UST", "market_type": "bond", "role": "primary"}],
    )
    broken = {"slot": "broken", "statement": "对等提供降低关税待遇。", "citations": []}
    generated(monkeypatch, {"claims": [framework, broken]})
    value = asyncio.run(_analyzer().extract(source, Budget.start(5)))
    [claim] = value.claims
    assert claim.slot == "framework" and claim.citations[0].quote in text
    assert claim.fields.phase == "unknown"
    assert [(asset.symbol, asset.market_type) for asset in claim.fields.assets] == [("UST", "unknown")]
    assert [(q.name, q.value) for q in claim.fields.quantities] == [("双方", "2")]
    assert [(row.slot, row.code) for row in value.discarded_claims] == [("broken", "news_claim_schema_invalid")]
    assert "news_extraction_claim_repaired index=0" in caplog.text


def test_only_an_answer_whose_every_claim_is_unusable_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    source = _headline_source("SENATE REFERS TETHER REPORT TO JUSTICE, TREASURY DEPTS: WSJ")
    generated(monkeypatch, {"claims": [_reply_claim("a", "Senate acts.", "Senate refers Tether to the FBI")]})
    with pytest.raises(ContractFault, match="news_citation_not_in_frozen_source"):
        asyncio.run(_analyzer().extract(source, Budget.start(5)))
