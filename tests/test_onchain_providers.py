from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
from decimal import Decimal
from urllib.parse import parse_qs

import httpx
import pytest
from eth_abi.abi import encode as encode_abi

from tracefold.integrations.onchain.binance import BinanceOnchainClient
from tracefold.integrations.onchain.dexscreener import DexScreenerOnchainDiscoveryClient
from tracefold.integrations.onchain.okx import OkxOnchainClient
from tracefold.integrations.onchain.oneinch import OneInchOnchainClient
from tracefold.trading.onchain import OnchainProviderUnavailable, OnchainQuoteRequest, resolve_onchain_candidates

NOW = 1_900_000_000_000
USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
HYPE = "0x1111111111111111111111111111111111111111"
BLUECHIP = "0xb200000000000000000000cfbdf64a8706a94a01"
BLUECHIP_FAKE = "0xb200000000000000000000166a9d351410e7009c"
COPPERINU = "0x5317c0d077d2eeb639448939b930d49c4984b63b"
COPPERINU_FAKE = "0xf46ec39a058e4fd98c4a32cdfaf09c8250eb9045"
WALLET = "0x7e5f4552091a69125d5dfcb7b8c2659029395bdf"
OKX_ROUTER = "0x8feab81d36e7576107d5de0758c1b839be31b4f6"
OKX_APPROVAL_PROXY = "0x40aa958dd87fc8305b97f2ba922cddca374bcd7f"


def _okx_swap_calldata(*, minimum: int = 990_000_000_000_000_000) -> str:
    encoded = encode_abi(
        [
            "uint256",
            "address",
            "(uint256,address,uint256,uint256,uint256)",
            "uint256[]",
            "(address[],address[],uint256[],bytes[],uint256)[][]",
            "(uint256,address,address,address,uint256,uint256,uint256,uint256,bool,bytes)[]",
        ],
        [
            1,
            WALLET,
            (int(USDC, 16), HYPE, 10_000_000, minimum, NOW // 1_000 + 300),
            [],
            [],
            [],
        ],
    )
    return "0x03b87e5f" + encoded.hex()


def test_okx_signs_token_directory_and_quote_requests_and_normalizes_response() -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/all-tokens"):
            return httpx.Response(
                200,
                json={
                    "code": "0",
                    "data": [
                        {
                            "tokenContractAddress": HYPE,
                            "tokenSymbol": "HYPE",
                            "tokenName": "Hyperliquid",
                            "decimals": "18",
                        }
                    ],
                },
            )
        return httpx.Response(
            200,
            json={
                "code": "0",
                "data": [
                    {
                        "chainIndex": "1",
                        "fromTokenAmount": "10000000",
                        "toTokenAmount": "1010000000000000000",
                        "fromToken": {
                            "tokenContractAddress": USDC,
                            "isHoneyPot": False,
                        },
                        "toToken": {
                            "tokenContractAddress": HYPE,
                            "isHoneyPot": False,
                        },
                        "estimateGasFee": "1002000",
                        "tradeFee": "0.13",
                        "priceImpactPercent": "-0.15",
                        "dexRouterList": [{"dexProtocol": {"dexName": "Uniswap V4"}}],
                    }
                ],
            },
        )

    client = OkxOnchainClient(
        api_key="okx-key",
        api_secret="okx-secret",
        passphrase="okx-passphrase",
        transport=httpx.MockTransport(handle),
        clock_ms=lambda: NOW,
    )

    async def exercise() -> tuple[object, object]:
        tokens = await client.search_tokens("HYPE", chain_ids=(1,))
        quote = await client.quote(
            OnchainQuoteRequest(
                chain_id=1,
                input_contract=USDC,
                output_contract=HYPE,
                input_amount_raw=10_000_000,
                slippage_bps=100,
            )
        )
        await client.close()
        return tokens, quote

    tokens, quote = asyncio.run(exercise())

    assert len(tokens) == 1 and tokens[0].provider == "okx" and tokens[0].verified is False
    assert quote.provider == "okx"
    assert quote.expected_output_raw == 1_010_000_000_000_000_000
    assert quote.price_impact_bps == 15
    assert quote.provider_fee_usd is None
    assert quote.gas_fee_usd == Decimal("0.13")
    assert quote.route_labels == ("Uniswap V4",)
    assert quote.risk_checked is True and quote.risk_blocked is False
    for request in requests:
        assert request.url.host == "web3.okx.com"
        assert request.url.path.startswith("/api/v6/dex/aggregator/")
        assert request.headers["OK-ACCESS-KEY"] == "okx-key"
        assert request.headers["OK-ACCESS-PASSPHRASE"] == "okx-passphrase"
        timestamp = request.headers["OK-ACCESS-TIMESTAMP"]
        prehash = f"{timestamp}GET{request.url.raw_path.decode()}"
        expected = base64.b64encode(hmac.new(b"okx-secret", prehash.encode(), hashlib.sha256).digest()).decode()
        assert request.headers["OK-ACCESS-SIGN"] == expected
        assert "okx-key" not in str(request.url) and "okx-secret" not in str(request.url)


def test_oneinch_searches_exact_symbol_and_quotes_with_bearer_credential() -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/token/v1.6/search"):
            return httpx.Response(
                200,
                json=[
                    {
                        "chainId": 1,
                        "symbol": "HYPE",
                        "name": "Hyperliquid",
                        "address": HYPE,
                        "decimals": 18,
                        "rating": 5,
                        "blacklisted": False,
                        "tradingRestricted": False,
                    },
                    {
                        "chainId": 1,
                        "symbol": "HYPE",
                        "name": "Blocked token",
                        "address": "0x3333333333333333333333333333333333333333",
                        "decimals": 18,
                        "rating": 5,
                        "blacklisted": True,
                    },
                    {
                        "chainId": 1,
                        "symbol": "HYPE",
                        "name": "Region-restricted token",
                        "address": "0x4444444444444444444444444444444444444444",
                        "decimals": 18,
                        "rating": 5,
                        "blacklisted": False,
                        "tradingRestricted": True,
                    },
                    {
                        "chainId": 1,
                        "symbol": "NOTHYPE",
                        "name": "Must not match",
                        "address": "0x2222222222222222222222222222222222222222",
                        "decimals": 18,
                        "rating": 5,
                        "blacklisted": False,
                    },
                ],
            )
        return httpx.Response(
            200,
            json={
                "srcToken": {"address": USDC},
                "dstToken": {"address": HYPE},
                "dstAmount": "990000000000000000",
                "gas": 180000,
                "protocols": [[[{"name": "Uniswap V3", "part": 100}]]],
            },
        )

    client = OneInchOnchainClient(
        api_key="oneinch-key",
        transport=httpx.MockTransport(handle),
        clock_ms=lambda: NOW,
    )

    async def exercise() -> tuple[object, object]:
        tokens = await client.search_tokens("HYPE", chain_ids=(1, 8453))
        quote = await client.quote(
            OnchainQuoteRequest(
                chain_id=1,
                input_contract=USDC,
                output_contract=HYPE,
                input_amount_raw=10_000_000,
                slippage_bps=100,
            )
        )
        await client.close()
        return tokens, quote

    tokens, quote = asyncio.run(exercise())

    assert len(tokens) == 1 and tokens[0].verified is True
    assert requests[0].url.path == "/token/v1.6/search"
    assert quote.expected_output_raw == 990_000_000_000_000_000
    assert quote.gas_limit == 180_000
    assert quote.route_labels == ("Uniswap V3",)
    for request in requests:
        assert request.url.host == "api.1inch.com"
        assert request.headers["Authorization"] == "Bearer oneinch-key"
        assert "oneinch-key" not in str(request.url)
    quote_query = parse_qs(requests[-1].url.query.decode())
    assert quote_query == {
        "src": [USDC],
        "dst": [HYPE],
        "amount": ["10000000"],
        "includeTokensInfo": ["true"],
        "includeProtocols": ["true"],
        "includeGas": ["true"],
    }


def test_okx_honeypot_quote_is_normalized_as_risk_blocked() -> None:
    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "code": "0",
                "data": [
                    {
                        "chainIndex": "1",
                        "fromTokenAmount": "10000000",
                        "toTokenAmount": "1010000000000000000",
                        "fromToken": {
                            "tokenContractAddress": USDC,
                            "isHoneyPot": False,
                        },
                        "toToken": {
                            "tokenContractAddress": HYPE,
                            "isHoneyPot": True,
                        },
                        "tradeFee": "0.13",
                        "priceImpactPercent": "0.15",
                        "dexRouterList": [],
                    }
                ],
            },
        )

    client = OkxOnchainClient(
        api_key="okx-key",
        api_secret="okx-secret",
        passphrase="okx-passphrase",
        transport=httpx.MockTransport(handle),
        clock_ms=lambda: NOW,
    )

    async def exercise() -> object:
        quote = await client.quote(
            OnchainQuoteRequest(
                chain_id=1,
                input_contract=USDC,
                output_contract=HYPE,
                input_amount_raw=10_000_000,
                slippage_bps=100,
            )
        )
        await client.close()
        return quote

    quote = asyncio.run(exercise())

    assert quote.risk_checked is True and quote.risk_blocked is True


def test_okx_execution_binds_provider_response_to_official_proxy_router_and_calldata() -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/approve-transaction"):
            approval_data = "0x095ea7b3" + OKX_APPROVAL_PROXY[2:].rjust(64, "0") + hex(10_000_000)[2:].rjust(64, "0")
            return httpx.Response(
                200,
                json={
                    "code": "0",
                    "data": [
                        {
                            "data": approval_data,
                            "dexContractAddress": OKX_APPROVAL_PROXY,
                            "gasLimit": "60000",
                            "gasPrice": "1000000000",
                        }
                    ],
                },
            )
        return httpx.Response(
            200,
            json={
                "code": "0",
                "data": [
                    {
                        "routerResult": {
                            "chainIndex": "1",
                            "fromTokenAmount": "10000000",
                            "toTokenAmount": "1000000000000000000",
                            "fromToken": {"tokenContractAddress": USDC, "isHoneyPot": False},
                            "toToken": {"tokenContractAddress": HYPE, "isHoneyPot": False},
                            "dexRouterList": [],
                        },
                        "tx": {
                            "from": WALLET,
                            "to": OKX_ROUTER,
                            "data": _okx_swap_calldata(),
                            "value": "0",
                            "gas": "240000",
                            "gasPrice": "1000000000",
                            "minReceiveAmount": "990000000000000000",
                            "slippagePercent": "1",
                        },
                    }
                ],
            },
        )

    client = OkxOnchainClient(
        api_key="okx-key",
        api_secret="okx-secret",
        passphrase="okx-passphrase",
        transport=httpx.MockTransport(handle),
        clock_ms=lambda: NOW,
    )

    async def exercise() -> object:
        plan = await client.prepare_execution(
            OnchainQuoteRequest(
                chain_id=1,
                input_contract=USDC,
                output_contract=HYPE,
                input_amount_raw=10_000_000,
                slippage_bps=100,
            ),
            wallet_address=WALLET,
        )
        await client.close()
        return plan

    plan = asyncio.run(exercise())

    assert plan.provider == "okx"
    assert plan.quote.minimum_output_raw == 990_000_000_000_000_000
    assert plan.approval is not None and plan.approval.to_address == USDC
    assert plan.swap.to_address == OKX_ROUTER
    swap_query = parse_qs(requests[1].url.query.decode())
    assert swap_query["swapReceiverAddress"] == [WALLET]


def test_okx_execution_rejects_a_provider_supplied_approval_proxy_drift() -> None:
    wrong_proxy = "0x3333333333333333333333333333333333333333"

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/approve-transaction"):
            return httpx.Response(
                200,
                json={
                    "code": "0",
                    "data": [
                        {
                            "data": "0x095ea7b3" + wrong_proxy[2:].rjust(64, "0") + hex(10_000_000)[2:].rjust(64, "0"),
                            "dexContractAddress": wrong_proxy,
                            "gasLimit": "60000",
                            "gasPrice": "1000000000",
                        }
                    ],
                },
            )
        return httpx.Response(
            200,
            json={
                "code": "0",
                "data": [
                    {
                        "routerResult": {
                            "chainIndex": "1",
                            "fromTokenAmount": "10000000",
                            "toTokenAmount": "1000000000000000000",
                            "fromToken": {"tokenContractAddress": USDC, "isHoneyPot": False},
                            "toToken": {"tokenContractAddress": HYPE, "isHoneyPot": False},
                        },
                        "tx": {
                            "from": WALLET,
                            "to": OKX_ROUTER,
                            "data": _okx_swap_calldata(),
                            "value": "0",
                            "gas": "240000",
                            "gasPrice": "1000000000",
                            "minReceiveAmount": "990000000000000000",
                            "slippagePercent": "1",
                        },
                    }
                ],
            },
        )

    client = OkxOnchainClient(
        api_key="okx-key",
        api_secret="okx-secret",
        passphrase="okx-passphrase",
        transport=httpx.MockTransport(handle),
        clock_ms=lambda: NOW,
    )

    async def exercise() -> None:
        with pytest.raises(OnchainProviderUnavailable) as caught:
            await client.prepare_execution(
                OnchainQuoteRequest(
                    chain_id=1,
                    input_contract=USDC,
                    output_contract=HYPE,
                    input_amount_raw=10_000_000,
                    slippage_bps=100,
                ),
                wallet_address=WALLET,
            )
        assert caught.value.code == "okx_execution_response_invalid"
        await client.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("provider", ["okx", "oneinch"])
def test_quote_response_identity_mismatch_is_rejected(provider: str) -> None:
    wrong = "0x5555555555555555555555555555555555555555"

    def handle(_request: httpx.Request) -> httpx.Response:
        if provider == "okx":
            return httpx.Response(
                200,
                json={
                    "code": "0",
                    "data": [
                        {
                            "chainIndex": "8453",
                            "fromTokenAmount": "10000000",
                            "toTokenAmount": "1000000000000000000",
                            "fromToken": {
                                "tokenContractAddress": USDC,
                                "isHoneyPot": False,
                            },
                            "toToken": {
                                "tokenContractAddress": HYPE,
                                "isHoneyPot": False,
                            },
                        }
                    ],
                },
            )
        return httpx.Response(
            200,
            json={
                "srcToken": {"address": USDC},
                "dstToken": {"address": wrong},
                "dstAmount": "1000000000000000000",
            },
        )

    client: OkxOnchainClient | OneInchOnchainClient
    if provider == "okx":
        client = OkxOnchainClient(
            api_key="okx-key",
            api_secret="okx-secret",
            passphrase="okx-passphrase",
            transport=httpx.MockTransport(handle),
            clock_ms=lambda: NOW,
        )
        expected_code = "okx_quote_response_invalid"
    else:
        client = OneInchOnchainClient(
            api_key="oneinch-key",
            transport=httpx.MockTransport(handle),
            clock_ms=lambda: NOW,
        )
        expected_code = "oneinch_quote_response_invalid"

    async def exercise() -> None:
        with pytest.raises(OnchainProviderUnavailable) as caught:
            await client.quote(
                OnchainQuoteRequest(
                    chain_id=1,
                    input_contract=USDC,
                    output_contract=HYPE,
                    input_amount_raw=10_000_000,
                    slippage_bps=100,
                )
            )
        assert caught.value.code == expected_code
        await client.close()

    asyncio.run(exercise())


def test_binance_uses_alpha_token_list_for_ca_evidence_but_not_as_a_web3_quote() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url == (
            "https://www.binance.com/bapi/defi/v1/public/wallet-direct/buw/wallet/cex/alpha/all/token/list"
        )
        return httpx.Response(
            200,
            json={
                "code": "000000",
                "data": [
                    {
                        "chainId": "1",
                        "chainName": "Ethereum",
                        "contractAddress": HYPE,
                        "symbol": "HYPE",
                        "name": "Hyperliquid",
                        "decimals": 18,
                        "offline": False,
                        "fullyDelisted": False,
                    },
                    {
                        "chainId": "56",
                        "chainName": "BSC",
                        "contractAddress": "0x2222222222222222222222222222222222222222",
                        "symbol": "OTHER",
                        "name": "Must not match",
                        "decimals": 18,
                        "offline": False,
                        "fullyDelisted": False,
                    },
                    {
                        "chainId": "1",
                        "chainName": "Ethereum",
                        "contractAddress": "0x3333333333333333333333333333333333333333",
                        "symbol": "HYPE",
                        "name": "Malformed status",
                        "decimals": 18,
                        "offline": "false",
                        "fullyDelisted": False,
                    },
                ],
            },
        )

    client = BinanceOnchainClient(transport=httpx.MockTransport(handle))

    async def exercise() -> None:
        tokens = await client.search_tokens("HYPE", chain_ids=(1, 8453))
        assert len(tokens) == 1
        assert tokens[0].provider == "binance"
        assert tokens[0].contract_address == HYPE
        assert tokens[0].verified is True
        with pytest.raises(OnchainProviderUnavailable) as caught:
            await client.quote(
                OnchainQuoteRequest(
                    chain_id=1,
                    input_contract=USDC,
                    output_contract=HYPE,
                    input_amount_raw=10_000_000,
                    slippage_bps=100,
                )
            )
        assert caught.value.code == "binance_general_web3_swap_api_unpublished"
        await client.close()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("query", "expected_symbol", "expected_chain", "expected_address"),
    [
        ("BLUECHIP", "BLUECHIP", 8453, BLUECHIP),
        (BLUECHIP, "BLUECHIP", 8453, BLUECHIP),
        ("COPPERINU", "COPPERINU", 4663, COPPERINU),
        (COPPERINU, "COPPERINU", 4663, COPPERINU),
    ],
)
def test_long_tail_discovery_resolves_ticker_or_ca_to_real_chain_contract(
    query: str,
    expected_symbol: str,
    expected_chain: int,
    expected_address: str,
) -> None:
    metadata = {
        BLUECHIP: ("BLUE CHIP", "BLUECHIP", 18),
        BLUECHIP_FAKE: ("Bluechip copy", "BLUECHIP", 18),
        COPPERINU: ("Copper Inu", "COPPERINU", 18),
        COPPERINU_FAKE: ("Copper Inu copy", "COPPERINU", 9),
    }
    all_pairs = {
        "BLUECHIP": [
            _dex_pair("base", BLUECHIP, "BLUE CHIP", "BLUECHIP", 300_000, "blue-1"),
            _dex_pair("base", BLUECHIP, "BLUE CHIP", "BLUECHIP", 200_000, "blue-2"),
            _dex_pair("base", BLUECHIP_FAKE, "Bluechip copy", "BLUECHIP", 100_000, "blue-fake"),
        ],
        "COPPERINU": [
            _dex_pair("robinhood", COPPERINU, "Copper Inu", "COPPERINU", 350_000, "copper-1"),
            _dex_pair("robinhood", COPPERINU, "Copper Inu", "COPPERINU", 250_000, "copper-2"),
            _dex_pair("robinhood", COPPERINU_FAKE, "Copper Inu copy", "COPPERINU", 180_000, "copper-fake"),
        ],
    }

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.dexscreener.com":
            if "/tokens/" in request.url.path:
                address = request.url.path.rsplit("/", maxsplit=1)[-1].lower()
                symbol = metadata[address][1]
                pairs = [pair for pair in all_pairs[symbol] if str(pair["baseToken"]["address"]).lower() == address]
            else:
                symbol = request.url.params["q"].upper()
                pairs = all_pairs[symbol]
            return httpx.Response(200, json={"pairs": pairs})
        calls = json.loads(request.content)
        address = str(calls[0]["params"][0]).lower()
        name, symbol, decimals = metadata[address]
        values = (
            "0xef",
            "0x" + encode_abi(["string"], [name]).hex(),
            "0x" + encode_abi(["string"], [symbol]).hex(),
            hex(decimals),
        )
        return httpx.Response(
            200,
            json=[{"jsonrpc": "2.0", "id": index, "result": value} for index, value in enumerate(values)],
        )

    client = DexScreenerOnchainDiscoveryClient(transport=httpx.MockTransport(handle))

    async def exercise() -> object:
        observations = await client.search_tokens(query, chain_ids=(8453, 4663))
        await client.close()
        return resolve_onchain_candidates(query, observations)

    candidates = asyncio.run(exercise())

    assert candidates[0].symbol == expected_symbol
    assert candidates[0].chain_id == expected_chain
    assert candidates[0].contract_address == expected_address
    assert candidates[0].verified is True
    assert candidates[0].pair_count == 2


def test_long_tail_discovery_reports_onchain_verification_outage_instead_of_silent_empty_result() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.dexscreener.com":
            return httpx.Response(
                200,
                json={"pairs": [_dex_pair("base", BLUECHIP, "BLUE CHIP", "BLUECHIP", 300_000, "blue-1")]},
            )
        return httpx.Response(
            200,
            json=[{"jsonrpc": "2.0", "id": index, "error": {"code": -32000}} for index in range(4)],
        )

    client = DexScreenerOnchainDiscoveryClient(transport=httpx.MockTransport(handle))

    async def exercise() -> None:
        with pytest.raises(OnchainProviderUnavailable) as caught:
            await client.search_tokens(BLUECHIP, chain_ids=(8453,))
        assert caught.value.code == "dexscreener_onchain_metadata_unavailable"
        await client.close()

    asyncio.run(exercise())


def _dex_pair(
    chain: str,
    address: str,
    name: str,
    symbol: str,
    liquidity_usd: int,
    pair_address: str,
) -> dict[str, object]:
    return {
        "chainId": chain,
        "pairAddress": pair_address,
        "baseToken": {"address": address, "name": name, "symbol": symbol},
        "quoteToken": {"address": "0x0000000000000000000000000000000000000000", "symbol": "ETH"},
        "liquidity": {"usd": liquidity_usd},
    }
