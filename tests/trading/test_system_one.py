"""Real DSPy decision translation over the official SDK's httpx2 transport."""

from __future__ import annotations

import asyncio
import time

import dspy
import httpx2
import pytest
from dspy.adapters.types.decision import Choice

from tracefold.app.system_one import SystemOneConnection


class ClaimSupport(dspy.Signature):
    claim: str = dspy.InputField()
    evidence: list[dict[str, str]] = dspy.InputField()
    verdict: Choice[
        ("supports", "The evidence supports the claim."),  # noqa: F722, F821, UP037
        ("contradicts", "The evidence contradicts the claim."),  # noqa: F722, F821, UP037
        ("mixed", "The evidence is mixed."),  # noqa: F722, F821, UP037
        ("insufficient", "The evidence is insufficient."),  # noqa: F722, F821, UP037
    ] = dspy.OutputField(desc="Decide whether the cited evidence supports this claim.")


def test_native_predict_choice_uses_sdk_state_questions_and_preserves_gateway_receipt() -> None:
    asyncio.run(_native_predict_choice())


async def _native_predict_choice() -> None:
    sent: list[dict] = []

    async def respond(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path == "/api/v1/systemone"
        assert request.headers["authorization"] == "Bearer example-test-key"
        import json

        payload = json.loads(request.content)
        sent.append(payload)
        return httpx2.Response(
            200,
            headers={"x-typesafe-request-id": "request-1"},
            json={
                "id": "generation-1",
                "provider": "typesafe",
                "model": "jev-2026-09-25",
                "usage": {"input_tokens": 12, "output_tokens": 3, "cost": 0.0002},
                "answers": {
                    "verdict": {
                        "type": "choice",
                        "choice": "supports",
                        "confidence": 0.8,
                        "probabilities": {
                            "supports": 0.8,
                            "contradicts": 0.1,
                            "mixed": 0.05,
                            "insufficient": 0.05,
                        },
                    }
                },
            },
        )

    connection = SystemOneConnection(
        base_url="https://openrouter.ai/api",
        api_key="example-test-key",
        model="jev-latest",
        async_transport=httpx2.MockTransport(respond),
    )
    try:
        lm = connection.bind()
        result = await dspy.Predict(ClaimSupport).acall(
            claim="The exchange halted withdrawals.",
            evidence=[{"ref": "event:1", "text": "Withdrawals are suspended."}],
            lm=lm,
        )
        assert result.verdict.value == "supports"
        assert sent[0]["model"] == "jev-latest"
        assert sent[0]["state"]["inputs"]["claim"] == "The exchange halted withdrawals."
        assert "verdict" in sent[0]["questions"]
        receipt = lm.history[0]
        assert receipt.requested_model == "jev-latest"
        assert receipt.served_model == "jev-2026-09-25"
        assert receipt.request_id == "generation-1"
        assert receipt.provider == "typesafe"
        assert receipt.cost_microusd == 200
    finally:
        await connection.aclose()


def test_native_sdk_does_not_dispatch_after_before_call_consumes_deadline() -> None:
    async def run() -> None:
        dispatched = 0
        receipts = []

        async def respond(_request: httpx2.Request) -> httpx2.Response:
            nonlocal dispatched
            dispatched += 1
            return httpx2.Response(500)

        async def before(_request: dict) -> None:
            await asyncio.sleep(0.03)

        async def after(receipt) -> None:
            receipts.append(receipt)

        connection = SystemOneConnection(
            base_url="https://fixture.invalid/api",
            api_key="fixture-key",
            model="jev-fixture",
            async_transport=httpx2.MockTransport(respond),
        )
        try:
            lm = connection.bind(
                before_call=before,
                after_call=after,
                deadline_at_monotonic=time.monotonic() + 0.01,
            )
            with pytest.raises(TimeoutError, match="system_one_deadline_before_dispatch"):
                await lm.acall(state={"inputs": {}}, questions={"verdict": {"type": "choice"}})
            assert dispatched == 0
            assert len(receipts) == 1 and not receipts[0].dispatched
        finally:
            await connection.aclose()

    asyncio.run(run())
