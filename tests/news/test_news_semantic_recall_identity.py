"""Retrieval policy versions results without discarding an extraction checkpoint."""

from __future__ import annotations

import asyncio
from typing import Any

from tests.support.news_update_semantic import MemoryCache, TaskBackend, update_one
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
