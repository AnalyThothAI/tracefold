from __future__ import annotations

import httpx
import pytest

from tracefold.integrations.gmgn.openapi_client import GmgnOpenApiClient, GmgnOpenApiError


def _client_with_error(message: str) -> GmgnOpenApiClient:
    return GmgnOpenApiClient(
        api_key="test-key",
        force_ipv4=False,
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={"code": 1, "message": message},
            )
        ),
    )


def test_token_info_invalid_argument_is_one_missing_item() -> None:
    client = _client_with_error(" invalid argument ")
    try:
        result = client.lookup_token_info(chain="ton", address="invalid-provider-address")
    finally:
        client.close()

    assert result.info is None


def test_other_token_info_errors_still_fail_closed() -> None:
    client = _client_with_error("unexpected provider contract change")
    try:
        with pytest.raises(GmgnOpenApiError, match="unexpected provider contract change"):
            client.lookup_token_info(chain="ton", address="invalid-provider-address")
    finally:
        client.close()
