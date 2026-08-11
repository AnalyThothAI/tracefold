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


def test_robinhood_lookup_normalizes_evm_address_without_changing_chain_identity() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "address": "0x020bfc650a365f8bb26819deaabf3e21291018b4",
                    "symbol": "STONKBROKER",
                },
            },
        )

    client = GmgnOpenApiClient(
        api_key="test-key",
        force_ipv4=False,
        transport=httpx.MockTransport(handler),
    )
    try:
        result = client.lookup_token_info(
            chain="robinhood",
            address="0x020BFc650A365f8BB26819dEAabF3e21291018b4",
        )
    finally:
        client.close()

    assert len(requests) == 1
    assert requests[0].url.params["chain"] == "robinhood"
    assert requests[0].url.params["address"] == "0x020bfc650a365f8bb26819deaabf3e21291018b4"
    assert result.info is not None
    assert result.info.chain == "robinhood"
    assert result.info.address == "0x020bfc650a365f8bb26819deaabf3e21291018b4"
