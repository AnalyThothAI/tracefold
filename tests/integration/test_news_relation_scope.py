"""Slot-scoped recall must survive semantic adoption and public outbox creation (#803)."""

from __future__ import annotations

import asyncio
import json

import pytest

from tests.support.news_0424_sql import NOTIFY_JOBS_SQL, SEMANTIC_RESULTS_SQL
from tests.support.news_update_pg import (
    EVENT,
    StubAnalyzer,
    TaskBackend,
    draft,
    run_agent,
    seed_event,
    sql,
    store,
)
from tracefold.news.storage.judgment_store import PgJudgmentCache
from tracefold.news.updates.contracts import Extraction, FrozenInput, PriorClaim, PublicUpdate
from tracefold.news.updates.judgment import BatchResult, NewsJudgments, Question, Task
from tracefold.news.updates.ports import PriorBatch
from tracefold.news.updates.semantics import SemanticAnalyzer
from tracefold.news.updates.service import NewsAgent

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("postgres_clone_dsn")]

CURRENT_STATEMENTS = ("Agency opens a new port.", "Agency starts a transport review.")


class TwoClaims:
    identity = "two-claim-relation-scope-test"

    async def extract(self, source: FrozenInput) -> Extraction:
        return Extraction(
            claims=tuple(
                draft(source.evidence[0], slot=slot, action=action, quote=statement)
                for slot, action, statement in zip(
                    ("a", "b"), ("opens port", "starts review"), CURRENT_STATEMENTS, strict=True
                )
            )
        )


class DisjointRecall:
    """Each new proposition has exactly one different historical comparison."""

    def __init__(self, first: PriorClaim, second: PriorClaim) -> None:
        self.by_slot = {"a": (first,), "b": (second,)}

    async def priors(self, source: FrozenInput, extracted: Extraction) -> PriorBatch:
        assert source.prior == () and {claim.slot for claim in extracted.claims} == set(self.by_slot)
        return PriorBatch(by_slot=self.by_slot, diagnostics={})


class UnrelatedBackend(TaskBackend):
    def __init__(self) -> None:
        super().__init__({"relation": "unrelated", "support": "supports"})
        self.relation_pairs: list[tuple[str, str]] = []

    async def judge(self, task: Task, items: tuple[Question, ...], *, context_json: str | None) -> BatchResult:
        if task == "relation":
            for item in items:
                payload = json.loads(item.payload_json)
                self.relation_pairs.append((payload["current"]["slot"], payload["previous"]["ref"]))
        return await super().judge(task, items, context_json=context_json)


def test_disjoint_recalled_priors_publish_both_supported_new_facts_without_changing_history() -> None:
    pg, db, clock = store()
    historical_events = ("ev-prior-a", "ev-prior-b")
    history_query = "SELECT * FROM news_analyses WHERE event_id = ANY(%s) ORDER BY analysis_id"

    async def run() -> None:
        historical_heads = []
        for event_id, statement, action in zip(
            historical_events,
            ("Agency renews an agricultural subsidy.", "Agency decreases an emissions tax."),
            ("renews subsidy", "decreases tax"),
            strict=True,
        ):
            seed_event(event_id, text=statement, title=statement, fingerprint=event_id)
            analyzer = StubAnalyzer(
                lambda source, action=action: Extraction(claims=(draft(source.evidence[0], action=action),))
            )
            agent = NewsAgent(pg.semantic, analyzer, program_identity="historical", clock=clock)
            assert await run_agent(agent, event_id) == "adopted"
            head = await pg.semantic.head(event_id)
            assert head is not None
            historical_heads.append(head)
        history_before = sql(history_query, (list(historical_events),))
        priors = tuple(
            PriorClaim(event_id=head.event_id, content_revision=head.content_revision, claim=head.claims[0])
            for head in historical_heads
        )

        seed_event(text=" ".join(CURRENT_STATEMENTS))
        backend = UnrelatedBackend()
        analyzer = SemanticAnalyzer(TwoClaims(), NewsJudgments(generated=backend, cache=PgJudgmentCache(db)))
        recall = DisjointRecall(*priors)
        agent = NewsAgent(pg.semantic, analyzer, program_identity="relation-scope", clock=clock, recall=recall)
        assert await run_agent(agent, EVENT) == "adopted"
        expected_pairs = {("a", priors[0].claim.ref), ("b", priors[1].claim.ref)}
        assert set(backend.relation_pairs) == expected_pairs and len(backend.relation_pairs) == 2

        head = await pg.semantic.head(EVENT)
        assert head is not None and head.input_revision == 1
        assert {claim.statement for claim in head.claims} == set(CURRENT_STATEMENTS)
        assert len(head.changes) == 2 and {change.kind for change in head.changes} == {"new_fact"}
        assert {change.current_ref for change in head.changes} == {claim.ref for claim in head.claims}
        (observation,) = sql(
            f"SELECT understanding,input_manifest FROM ({SEMANTIC_RESULTS_SQL}) WHERE event_id=%s", (EVENT,)
        )
        understood = Extraction.model_validate(observation["understanding"])
        assert {(row.slot, row.previous_ref) for row in understood.relations} == expected_pairs
        assert {row.relation for row in understood.relations} == {"unrelated"}
        assert observation["input_manifest"]["recall"]["pair_count"] == 2

        (outbox,) = sql(
            "SELECT kind,source_fact_key,source_revision,payload,acknowledged_at_ms FROM news_trade_events "
            "WHERE source_fact_key=%s",
            (EVENT,),
        )
        public = PublicUpdate.model_validate(outbox["payload"])
        assert (outbox["kind"], public.kind) == ("catalyst", "catalyst_delta")
        assert outbox["source_revision"] == public.content_revision == head.content_revision
        assert outbox["source_fact_key"] == public.event_id == EVENT and outbox["acknowledged_at_ms"] is None
        assert {claim.statement for claim in public.claims} == set(CURRENT_STATEMENTS)
        assert len(public.changes) == 2 and {change.kind for change in public.changes} == {"new_fact"}
        assert set(public.claim_refs) == {claim.ref for claim in head.claims}
        assert sql(f"SELECT state,content_revision FROM ({NOTIFY_JOBS_SQL}) WHERE event_id=%s", (EVENT,)) == [
            {"state": "pending", "content_revision": head.content_revision}
        ]
        assert EVENT in await pg.notifications.pending_notification_events("news", limit=64)
        assert sql(history_query, (list(historical_events),)) == history_before
        for historical in historical_heads:
            assert await pg.semantic.head(historical.event_id) == historical

    asyncio.run(run())
