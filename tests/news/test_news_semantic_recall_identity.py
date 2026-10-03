"""Retrieval policy versions results without discarding an extraction checkpoint."""

from __future__ import annotations

import asyncio
from typing import Any

from tests.support.news_update_semantic import MemoryCache, TaskBackend, update_one
from tracefold.news.adapters.extraction import EXTRACTION_INSTRUCTION, DspyExtractor
from tracefold.news.claim_recall import embed_text, text_sha
from tracefold.news.notifications.reader import ReaderInput, cache_key
from tracefold.news.updates.assembly import assemble_update
from tracefold.news.updates.contracts import Extraction, FrozenInput, SemanticLease
from tracefold.news.updates.judgment import NewsJudgments
from tracefold.news.updates.ports import SemanticCheckpoint
from tracefold.news.updates.semantics import SemanticAnalyzer
from tracefold.news.updates.service import NewsAgent


class Extractor:
    identity = "fixed-reader"

    def __init__(self, extraction: Extraction) -> None:
        self.extraction = extraction
        self.calls = 0

    async def extract(self, source: FrozenInput) -> Extraction:
        self.calls += 1
        return self.extraction


class Store:
    def __init__(self) -> None:
        self.checkpoints: dict[str, Extraction] = {}
        self.observations: dict[str, Any] = {}
        self.adopt = False

    async def head(self, event_id):
        return None

    async def checkpoint(self, work_id):
        extraction = self.checkpoints.get(work_id)
        return None if extraction is None else SemanticCheckpoint(work_id=work_id, extraction=extraction)

    async def save_extraction(self, work_id, extracted):
        return self.checkpoints.setdefault(work_id, extracted)

    async def save_observation(self, observation):
        return self.observations.setdefault(observation.result_id, observation)

    async def atomic_adopt(self, **kwargs):
        return self.adopt

    async def finish_semantic_work(self, work_id, *, lease, reason):
        pass

    async def defer_semantic_event(self, lease, *, reason):
        pass


def test_changed_extraction_instruction_cannot_reuse_old_work_or_checkpoint() -> None:
    old = DspyExtractor(lambda: None, model_identity="fixed", topics={}, instruction="Old frozen instruction.")
    new = DspyExtractor(lambda: None, model_identity="fixed", topics={})
    assert old.identity != new.identity
    assert new.instruction == EXTRACTION_INSTRUCTION
    source, extraction, _ = update_one()
    judgments = NewsJudgments(generated=TaskBackend(), cache=MemoryCache())
    extractors = [Extractor(extraction), Extractor(extraction)]
    for controlled, program in zip(extractors, (old, new), strict=True):
        controlled.identity = program.identity
    analyzers = [SemanticAnalyzer(extractor, judgments, topics=()) for extractor in extractors]
    assert analyzers[0].identity != analyzers[1].identity
    store = Store()
    lease = SemanticLease(source=source, lease_token="lease", attempts=1)
    for analyzer in analyzers:
        agent = NewsAgent(store, analyzer, program_identity="fixed")
        assert asyncio.run(agent.process(lease)) == "deferred"
    assert len(store.checkpoints) == 2
    assert [row.calls for row in extractors] == [1, 1]
    assert len({row.work_id for row in store.observations.values()}) == 2


def test_statement_completion_keeps_fact_identity_but_changes_vector_and_reader_cache() -> None:
    source, extraction, head = update_one()
    old = extraction.claims[0]
    completed = old.model_copy(update={"statement": "Agency will impose a 25% steel import tariff on October 1."})
    # Same structured proposition; no assumption that every wording has a new stable claim ref.
    update = assemble_update(source, Extraction(claims=(completed,)), None, adopted_at_ms=head.adopted_at_ms)
    assert update is not None
    before, after = head.claims[0], update.claims[0]
    assert before.ref == after.ref
    assert embed_text(after) == after.statement
    assert text_sha(before) != text_sha(after)
    judge = Extractor(extraction)
    reader = ReaderInput.of(after, update, [])
    assert cache_key(judge, ReaderInput.of(before, head, [])) != cache_key(judge, reader)
    assert cache_key(judge, reader) != cache_key(judge, ReaderInput.of(after, update, ["Earlier delivered body"]))
    changed_fields = after.model_copy(update={"fields": after.fields.model_copy(update={"speaker": "Agency"})})
    assert cache_key(judge, reader) != cache_key(judge, ReaderInput.of(changed_fields, update, []))
    changed_quote = after.model_copy(
        update={"citations": (after.citations[0].model_copy(update={"quote": "Agency announces"}),)}
    )
    assert cache_key(judge, reader) != cache_key(judge, ReaderInput.of(changed_quote, update, []))


def test_a_recall_policy_change_reuses_checkpoint_but_has_a_new_immutable_result(monkeypatch) -> None:
    source, extraction, _ = update_one()
    extractor, store = Extractor(extraction), Store()
    judgments = NewsJudgments(generated=TaskBackend(), cache=MemoryCache())
    analyzer = SemanticAnalyzer(extractor, judgments, topics=())
    lease = SemanticLease(source=source, lease_token="lease", attempts=1)
    monkeypatch.setattr("tracefold.news.updates.service.RECALL_POLICY", "recall-before")
    subject = NewsAgent(store, analyzer, program_identity="program", clock=lambda: 100)  # type: ignore[arg-type]
    assert asyncio.run(subject.process(lease)) == "deferred"
    before = next(iter(store.observations.values()))
    monkeypatch.setattr("tracefold.news.updates.service.RECALL_POLICY", "recall-after")
    monkeypatch.setattr("tracefold.news.claim_recall.RECALL_POLICY", "recall-after")
    next_analyzer = SemanticAnalyzer(extractor, judgments, topics=())
    assert next_analyzer.identity == analyzer.identity
    subject.analyzer = next_analyzer
    store.adopt = True
    assert asyncio.run(subject.process(lease)) == "adopted"
    assert extractor.calls == len(store.checkpoints) == 1
    assert len(store.observations) == 2
    after = next(row for row in store.observations.values() if row.result_id != before.result_id)
    assert before.work_id == after.work_id
    assert before.input_manifest["recall"]["policy"] == "recall-before"
    assert after.input_manifest["recall"]["policy"] == "recall-after"


def test_an_empty_input_still_records_the_recall_policy_and_versions_its_result(monkeypatch) -> None:
    source = FrozenInput(event_id="empty", revision=1, lineage_id="line", evidence=())
    extractor, store = Extractor(Extraction(claims=())), Store()
    analyzer = SemanticAnalyzer(extractor, NewsJudgments(generated=TaskBackend(), cache=MemoryCache()), topics=())
    subject = NewsAgent(store, analyzer, program_identity="program", clock=lambda: 100)  # type: ignore[arg-type]
    lease = SemanticLease(source=source, lease_token="lease", attempts=1)
    for policy in ("recall-before", "recall-after"):
        monkeypatch.setattr("tracefold.news.updates.service.RECALL_POLICY", policy)
        assert asyncio.run(subject.process(lease)) == "unchanged"
    assert len(store.observations) == 2 and extractor.calls == 0
    assert {row.input_manifest["recall"]["policy"] for row in store.observations.values()} == {
        "recall-before",
        "recall-after",
    }
    assert all(row.input_manifest["recall"]["queries"] == {} for row in store.observations.values())
    assert all(row.input_manifest["recall"]["pair_count"] == 0 for row in store.observations.values())
