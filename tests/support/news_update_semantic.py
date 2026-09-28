"""Small semantic source and generated transport fixtures shared by News tests."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from tracefold.news.updates import dspy_backend
from tracefold.news.updates.contracts import (
    DraftClaim,
    EventUpdate,
    Evidence,
    Extraction,
    FrozenInput,
    PriorClaim,
    Source,
    SupportDraft,
)
from tracefold.news.updates.judgment import Answer, BatchResult, ProviderUnavailable, Question, Task
from tracefold.news.updates.semantics import assemble_update

STAMP = 1_790_405_000_000


class MemoryCache:
    def __init__(self) -> None:
        self.values: dict[str, Answer] = {}

    async def get(self, key: str) -> Answer | None:
        return self.values.get(key)

    async def put(self, key: str, answer: Answer) -> None:
        self.values.setdefault(key, answer)


class TaskBackend:
    """Answer per judgment task and record each submitted item."""

    def __init__(self, values: dict[Task, str | bool] | None = None, *, identity: str = "generated") -> None:
        self.identity = identity
        self.values = values or {}
        self.calls: list[tuple[Task, tuple[str, ...]]] = []

    async def judge(self, task: Task, items: tuple[Question, ...], *, context_json: str | None) -> BatchResult:
        self.calls.append((task, tuple(item.item_id for item in items)))
        if task not in self.values:
            raise ProviderUnavailable("controlled provider failure")
        value = self.values[task]
        return BatchResult(
            answers=tuple(Answer(item_id=item.item_id, value=value, backend=self.identity) for item in items)
        )


def material(text: str, *, revision: int = 1, publisher: str = "wire") -> Evidence:
    return Evidence.issue(
        text,
        Source(
            publisher_id=publisher,
            artifact_id="release-1",
            artifact_revision=str(revision),
            first_available_at_ms=STAMP + revision,
            origin_id="issuer",
        ),
    )


def draft(
    evidence: Evidence,
    *,
    quantity: str = "25",
    mode: str = "decision",
    phase: str = "announced",
    content_kind: str = "official_measure",
) -> DraftClaim:
    return DraftClaim.model_validate(
        {
            "slot": "a",
            "statement": evidence.text,
            "fields": {
                "subject": "Agency",
                "action": "set tariff",
                "object": "imports",
                "mode": mode,
                "phase": phase,
                "content_kind": content_kind,
                "effective_at": "2026-10-01",
                "quantities": [{"name": "rate", "value": quantity, "unit": "%"}],
                "assets": [{"symbol": "CL", "market_type": "commodity", "role": "primary"}],
            },
            "citations": [{"evidence_ref": evidence.ref, "quote": evidence.text}],
        }
    )


def update_one() -> tuple[FrozenInput, Extraction, EventUpdate]:
    evidence = material("Agency announces 25% tariff effective October 1.")
    source = FrozenInput(event_id="event-1", revision=1, lineage_id="line-1", evidence=(evidence,))
    extracted = Extraction(
        claims=(draft(evidence),),
        supports=(SupportDraft(slot="a", evidence_ref=evidence.ref, relation="reports"),),
    )
    update = assemble_update(source, extracted, None, adopted_at_ms=STAMP + 5)
    assert update is not None
    return source, extracted, update


def prior_of(head: EventUpdate) -> tuple[PriorClaim, ...]:
    return tuple(
        PriorClaim(event_id=head.event_id, content_revision=head.content_revision, claim=claim) for claim in head.claims
    )


def generated(monkeypatch: pytest.MonkeyPatch, reply: Any) -> list[dict[str, Any]]:
    """Record the input the production DSPy adapter actually sends."""

    calls: list[dict[str, Any]] = []

    async def answer(signature: Any, route: Any, **inputs: Any) -> Any:
        calls.append(inputs)
        value = reply(inputs) if callable(reply) else reply
        return SimpleNamespace(result=signature.output_fields["result"].annotation.model_validate(value))

    monkeypatch.setattr(dspy_backend, "_generate", answer)
    return calls
