"""Receipt identities come from the real SDK response and error boundaries."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx2
import pytest
from typesafe_sdk import TypeSafeAPIError

from tracefold.app.system_one import SystemOneConnection, receipt_json


def _questions() -> dict[str, Any]:
    return {
        "verdict": {
            "type": "choice",
            "instructions": "Does the supplied evidence support the claim?",
            "criteria": {"supports": "Supported by the evidence.", "unsupported": "Not supported."},
        }
    }


def _response() -> dict[str, Any]:
    return {
        "model": "jev-1.13.0",
        "usage": {"input_tokens": 12, "output_tokens": 3},
        "answers": {
            "verdict": {
                "type": "choice",
                "choice": "supports",
                "confidence": 0.8,
                "probabilities": {"supports": 0.8, "unsupported": 0.2},
            }
        },
    }


@pytest.mark.parametrize(
    ("headers", "expected_id"),
    [
        ({"x-typesafe-request-id": "native-request"}, "native-request"),
        ({"x-request-id": "gateway-request"}, "gateway-request"),
        ({"x-typesafe-request-id": "native-request", "x-request-id": "gateway-request"}, "native-request"),
        ({}, None),
    ],
)
def test_native_response_identity_without_an_openai_body_id(
    headers: dict[str, str], expected_id: str | None
) -> None:
    calls: list[httpx2.Request] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        calls.append(request)
        assert request.url.path == "/v1/systemone"
        return httpx2.Response(200, headers=headers, json=_response())

    connection = SystemOneConnection(
        base_url="https://api.typesafe.ai",
        api_key="example-test-key",
        model="jev-1.13.0",
        sync_transport=httpx2.MockTransport(respond),
    )
    try:
        lm = connection.bind()
        result = lm(state={"claim": "Withdrawals stopped."}, questions=_questions())
        assert result["verdict"]["choice"] == "supports"
        assert len(calls) == 1
        receipt = lm.history[0]
        assert receipt.request_id == expected_id
        assert receipt.requested_model == "jev-1.13.0"
        assert receipt.served_model == "jev-1.13.0"
        assert receipt.input_tokens == 12
        assert receipt.output_tokens == 3
        assert receipt.cost_microusd is None
        assert receipt.error_type is None
        assert "example-test-key" not in receipt_json(receipt)
    finally:
        asyncio.run(connection.aclose())


@pytest.mark.parametrize("status", [401, 403, 429, 500, 529])
def test_sdk_error_keeps_request_identity_and_does_not_retry(status: int) -> None:
    calls: list[httpx2.Request] = []
    body = {"error": {"message": "Provider rejected this request."}}

    def respond(request: httpx2.Request) -> httpx2.Response:
        calls.append(request)
        return httpx2.Response(
            status,
            headers={"x-typesafe-request-id": "failed-request", "set-cookie": "provider-secret"},
            json=body,
        )

    connection = SystemOneConnection(
        base_url="https://api.typesafe.ai",
        api_key="example-test-key",
        model="jev-1.13.0",
        sync_transport=httpx2.MockTransport(respond),
    )
    try:
        lm = connection.bind()
        with pytest.raises(TypeSafeAPIError) as raised:
            lm(state={"claim": "Withdrawals stopped."}, questions=_questions())
        assert len(calls) == 1
        assert len(lm.history) == 1
        receipt = lm.history[0]
        assert receipt.request_id == "failed-request"
        assert receipt.response_payload == body
        assert receipt.error_type == type(raised.value).__name__
        assert receipt.served_model is None
        assert receipt.cost_microusd is None
        assert receipt.input_tokens is None
        assert receipt.output_tokens is None
        assert "example-test-key" not in receipt_json(receipt)
        assert "provider-secret" not in receipt_json(receipt)
    finally:
        asyncio.run(connection.aclose())


def test_cancelled_native_call_is_not_replaced_by_a_receipt_error() -> None:
    asyncio.run(_cancelled_native_call())


async def _cancelled_native_call() -> None:
    calls: list[httpx2.Request] = []

    async def respond(request: httpx2.Request) -> httpx2.Response:
        calls.append(request)
        raise asyncio.CancelledError

    connection = SystemOneConnection(
        base_url="https://api.typesafe.ai",
        api_key="example-test-key",
        model="jev-1.13.0",
        async_transport=httpx2.MockTransport(respond),
    )
    try:
        lm = connection.bind()
        with pytest.raises(asyncio.CancelledError):
            await lm.acall(state={"claim": "Withdrawals stopped."}, questions=_questions())
        assert len(calls) == 1
        assert len(lm.history) == 1
        receipt = lm.history[0]
        assert receipt.error_type == "CancelledError"
        assert receipt.request_id is None
        assert receipt.response_payload is None
        assert receipt.cost_microusd is None
    finally:
        await connection.aclose()
