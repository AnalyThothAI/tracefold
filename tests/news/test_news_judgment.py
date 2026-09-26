"""Native DSPy + official SDK boundary, task contracts and partial-batch recovery."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from typing import Any

import httpx2
import pytest
from dspy.adapters.types.decision import Choice, Noul
from dspy.utils import DummyLM

from tracefold.app.system_one import SystemOneConnection
from tracefold.news.judgment import (
    BATCH_ITEMS,
    TASK_VERSIONS,
    JudgmentBatchResult,
    JudgmentConfigurationError,
    JudgmentDeadline,
    JudgmentItem,
)
from tracefold.news.program.judgment import (
    GenerativeNewsJudgmentBackend,
    NativeNewsJudgmentBackend,
    decision_signature,
    generative_signature,
)


def _items(n: int) -> tuple[JudgmentItem, ...]:
    return tuple(JudgmentItem(item_id=f"claim-{i}", payload={"text": f"claim {i}"}) for i in range(n))


def _reply(payload: dict[str, Any], *, value: str = "full") -> httpx2.Response:
    return httpx2.Response(
        200,
        headers={"x-typesafe-request-id": "native-request"},
        json={
            "id": "gateway-request",
            "model": "jev-1.13.0",
            "usage": {"input_tokens": 123, "output_tokens": 0, "cost": 0.000123},
            "answers": {
                name: {
                    "type": "choice",
                    "choice": value,
                    "confidence": 0.8,
                    "probabilities": {key: (0.8 if key == value else 0.05) for key in question["criteria"]},
                }
                for name, question in payload["questions"].items()
            },
        },
    )


class _Fallback(GenerativeNewsJudgmentBackend):
    def __init__(self) -> None:
        self.batches: list[tuple[str, ...]] = []
        super().__init__(self._lm)

    def _lm(self, remaining: float) -> DummyLM:
        assert remaining > 0
        return DummyLM([{f"item_{i}_coverage": "none" for i in range(BATCH_ITEMS)}])

    async def _batch(self, **kwargs: Any) -> JudgmentBatchResult:
        self.batches.append(tuple(item["item_id"] for item in kwargs["inputs"]))
        n = len(kwargs["inputs"])
        backend = GenerativeNewsJudgmentBackend(lambda _: DummyLM([{f"item_{i}_coverage": "none" for i in range(n)}]))
        return await backend._batch(**kwargs)


def _connection(respond: Callable) -> SystemOneConnection:
    return SystemOneConnection(
        base_url="https://openrouter.ai/api",
        api_key="unit-test-key",
        model="jev-1.13",
        async_transport=httpx2.MockTransport(respond),
    )


async def _judge(backend: Any, n: int, **kwargs: Any) -> JudgmentBatchResult:
    return await backend.judge_batch(
        task_kind="coverage",
        task_version=TASK_VERSIONS["coverage"],
        frozen_evidence={},
        ordered_items=_items(n),
        deadline=kwargs.pop("deadline", JudgmentDeadline.start(total_at=time.monotonic() + 10)),
        **kwargs,
    )


def test_native_batches_preserve_all_items_and_do_not_vote_again() -> None:
    async def run() -> None:
        sent: list[dict] = []

        async def respond(request: httpx2.Request) -> httpx2.Response:
            assert request.url.path == "/api/v1/systemone"
            payload = json.loads(request.content)
            assert set(payload) == {"state", "questions", "model"}
            sent.append(payload)
            return _reply(payload)

        connection = _connection(respond)
        fallback = _Fallback()
        try:
            backend = NativeNewsJudgmentBackend(
                lambda seconds: connection.bind(timeout_seconds=seconds), fallback=fallback
            )
            result = await _judge(backend, 19)
            assert [len(request["state"]["inputs"]["items"]) for request in sent] == [8, 8, 3]
            assert [item.item_id for item in result.results] == [f"claim-{i}" for i in range(19)]
            assert all(item.value == {"coverage": "full"} for item in result.results)
            assert all(item.backend == "native" for item in result.results)
            assert fallback.batches == []
            assert len(result.calls) == 3
            assert result.calls[0].requested_model == "jev-1.13"
            assert result.calls[0].served_model == "jev-1.13.0"
            assert result.calls[0].provider_request_id == "gateway-request"
            assert result.calls[0].input_tokens == 123
            assert result.calls[0].cost_microusd == 123
            assert result.results[0].probabilities["coverage"]["full"] == 0.8
            assert result.results[0].confidences["coverage"] == 0.8
        finally:
            await connection.aclose()

    asyncio.run(run())


def test_each_native_field_description_points_to_its_actual_input_slot() -> None:
    signature = decision_signature("claim_relation", 2, TASK_VERSIONS["claim_relation"])
    for name, field in signature.output_fields.items():
        index = int(name.split("_")[1])
        assert f"inputs.items[{index}]" in field.json_schema_extra["desc"]
        assert issubclass(field.annotation, (Choice, Noul))
    assert len(signature.output_fields) == 6
    assert signature is decision_signature("claim_relation", 2, TASK_VERSIONS["claim_relation"])


def test_fallback_contract_has_plain_typed_answers_and_no_probability_types() -> None:
    native = decision_signature("claim_relation", 1, TASK_VERSIONS["claim_relation"])
    generated = generative_signature("claim_relation", 1, TASK_VERSIONS["claim_relation"])
    assert native is not generated
    assert generated.output_fields["item_0_scope_changed"].annotation is bool
    assert "source_correction" in str(generated.output_fields["item_0_cause"].annotation)
    assert "Choice" not in str(generated.output_fields["item_0_relation"].annotation)


def test_only_failed_native_chunk_uses_one_generative_fallback() -> None:
    async def run() -> None:
        count = 0

        async def respond(request: httpx2.Request) -> httpx2.Response:
            nonlocal count
            count += 1
            if count == 2:
                return httpx2.Response(429, json={"error": "rate limited"})
            return _reply(json.loads(request.content))

        connection = _connection(respond)
        fallback = _Fallback()
        checkpoints: list[JudgmentBatchResult] = []

        async def save(result: JudgmentBatchResult) -> None:
            checkpoints.append(result)

        try:
            backend = NativeNewsJudgmentBackend(
                lambda seconds: connection.bind(timeout_seconds=seconds), fallback=fallback
            )
            result = await _judge(backend, 18, checkpoint=save)
            assert count == 3  # SDK retry is zero.
            assert fallback.batches == [tuple(f"claim-{i}" for i in range(8, 16))]
            assert len(result.results) == 18
            assert [item.backend for item in result.results] == ["native"] * 8 + ["generative"] * 8 + ["native"] * 2
            assert len(result.calls) == 4
            assert len(checkpoints) == 3
            assert result.results[8].probabilities == {}  # No fake confidence from a generative answer.
        finally:
            await connection.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("status", [400, 401, 403])
def test_bad_configuration_is_explicit_and_never_uses_fallback(status: int) -> None:
    async def run() -> None:
        count = 0

        async def respond(_: httpx2.Request) -> httpx2.Response:
            nonlocal count
            count += 1
            return httpx2.Response(status, json={"error": "configuration"})

        connection = _connection(respond)
        fallback = _Fallback()
        try:
            backend = NativeNewsJudgmentBackend(
                lambda seconds: connection.bind(timeout_seconds=seconds), fallback=fallback
            )
            with pytest.raises(JudgmentConfigurationError):
                await _judge(backend, 1)
            assert count == 1
            assert fallback.batches == []
        finally:
            await connection.aclose()

    asyncio.run(run())


def test_unresolved_answer_is_not_a_provider_error_or_implicit_drop() -> None:
    async def run() -> None:
        async def respond(request: httpx2.Request) -> httpx2.Response:
            return _reply(json.loads(request.content), value="unresolved")

        connection = _connection(respond)
        fallback = _Fallback()
        try:
            backend = NativeNewsJudgmentBackend(
                lambda seconds: connection.bind(timeout_seconds=seconds), fallback=fallback
            )
            result = await _judge(backend, 1)
            assert result.results[0].status == "unresolved"
            assert result.results[0].value == {"coverage": "unresolved"}
            assert fallback.batches == []
        finally:
            await connection.aclose()

    asyncio.run(run())


def test_empty_candidate_batch_makes_no_call_even_with_expired_budget() -> None:
    fallback = _Fallback()
    backend = NativeNewsJudgmentBackend(lambda _: pytest.fail("no candidate, no LM"), fallback=fallback)
    result = asyncio.run(_judge(backend, 0, deadline=JudgmentDeadline(total_at=0, native_at=0)))
    assert result.results == () and result.calls == ()
    assert fallback.batches == []


def test_expired_native_budget_is_shared_and_does_not_reset_for_each_chunk() -> None:
    fallback = _Fallback()
    backend = NativeNewsJudgmentBackend(lambda _: pytest.fail("native budget already used"), fallback=fallback)
    result = asyncio.run(_judge(backend, 10, deadline=JudgmentDeadline(total_at=time.monotonic() + 10, native_at=0)))
    assert len(fallback.batches) == 2
    assert all(item.backend == "generative" for item in result.results)


def test_total_deadline_and_cancellation_keep_previous_checkpoints_without_extra_calls() -> None:
    async def run(cancel: bool) -> None:
        entered = asyncio.Event()
        count = 0
        cancelled = asyncio.Event()

        async def respond(request: httpx2.Request) -> httpx2.Response:
            nonlocal count
            count += 1
            if count == 1:
                return _reply(json.loads(request.content))
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
            raise AssertionError("never reaches a synthetic completion")

        saved: list[JudgmentBatchResult] = []

        async def checkpoint(result: JudgmentBatchResult) -> None:
            saved.append(result)

        connection = _connection(respond)
        fallback = _Fallback()
        try:
            backend = NativeNewsJudgmentBackend(
                lambda seconds: connection.bind(timeout_seconds=seconds), fallback=fallback
            )
            until = time.monotonic() + (10 if cancel else 0.1)
            task = asyncio.create_task(
                _judge(backend, 10, deadline=JudgmentDeadline(until, until), checkpoint=checkpoint)
            )
            await entered.wait()
            if cancel:
                task.cancel()
            with pytest.raises(asyncio.CancelledError if cancel else TimeoutError):
                await task
            assert cancelled.is_set()
            assert count == 2
            assert fallback.batches == []
            assert len(saved) == 1 and len(saved[0].results) == 8
        finally:
            await connection.aclose()

    asyncio.run(run(False))
    asyncio.run(run(True))


def test_completed_dependency_cache_reuses_only_matching_inputs() -> None:
    async def run() -> None:
        fallback = _Fallback()
        backend = NativeNewsJudgmentBackend(lambda _: pytest.fail("budget expired"), fallback=fallback)
        deadline = JudgmentDeadline(total_at=time.monotonic() + 10, native_at=0)
        result = await _judge(backend, 2, deadline=deadline)
        cache = {item.dependency_sha256: item for item in result.results}
        again = await _judge(backend, 2, cached=cache, deadline=deadline)
        assert again.calls == () and len(fallback.batches) == 1
        changed = await backend.judge_batch(
            task_kind="coverage",
            task_version=TASK_VERSIONS["coverage"],
            frozen_evidence={},
            ordered_items=(_items(2)[0], JudgmentItem(item_id="claim-1", payload={"text": "revised"})),
            deadline=deadline,
            cached=cache,
        )
        assert fallback.batches[-1] == ("claim-1",)
        assert changed.results[0].dependency_sha256 == result.results[0].dependency_sha256
        assert changed.results[1].dependency_sha256 != result.results[1].dependency_sha256

    asyncio.run(run())


@pytest.mark.parametrize("total,native", [(float("inf"), 2), (1, float("nan")), (1, -1)])
def test_nonfinite_and_invalid_stage_budgets_are_rejected(total: float, native: float) -> None:
    with pytest.raises(ValueError, match="deadline_invalid"):
        JudgmentDeadline.start(total_at=total, native_seconds=native)
