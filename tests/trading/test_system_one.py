"""Real DSPy decision translation over the official SDK's httpx2 transport."""

from __future__ import annotations

import asyncio

import dspy
import httpx2
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
