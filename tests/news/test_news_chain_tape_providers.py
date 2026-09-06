"""The two read-only provider adapters, against the responses the real endpoints returned (#572 PR-1).

Every payload here was recorded from the live services on 2026-09-06 and is replayed verbatim through
`httpx.MockTransport`, so what is exercised is the adapter's own parsing, its bounded failure
vocabulary and the header the public RPC actually requires -- not a hand-written idea of their shapes.
"""

from __future__ import annotations

import asyncio
import json
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from tracefold.integrations.dexscreener import DexScreenerClient, DexScreenerError
from tracefold.integrations.robinhood_chain import (
    CHAIN_RPC_USER_AGENT,
    ROBINHOOD_CHAIN_ID,
    ChainRpcError,
    RobinhoodChainClient,
)
from tracefold.integrations.robinhoodtrenches import (
    RobinhoodTrenchesClient,
    RosterProviderError,
)
from tracefold.news.chain_tape.classify import TRANSFER_TOPIC
from tracefold.news.chain_tape.evm import address_topic
from tracefold.news.chain_tape.rules import ratio_bps

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "chain_tape"

SELL_TX = "0x5c10c3cf9b3a5ef265de9ea87e0b4c787583ef11823ea233fde27528ab9ac5f0"
SELL_WALLET = "0x69326e48f68500fb6cf3b3a7da640737b9cc347b"
STABLE = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
FSD = "0x8de9018c1bb82884245f06dede9fe2bebabd1e18"
# The block before the recorded sell, and the `balanceOf` selector the exit rule reads it with.
FSD_BALANCE_BLOCK = hex(55_432_993)
_BALANCE_OF = "0x70a08231"


def _fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _rpc_transport(seen: list[dict[str, Any]] | None = None) -> httpx.MockTransport:
    """Replay the recorded answers, keyed by JSON-RPC method and argument."""

    receipts = {
        SELL_TX: _fixture("receipt_sell_fsd.json"),
        "0x42f41c071eb8a6483995fe817b6ff8289f9b4a96ad2add4e6a9362dcfc23742b": _fixture("receipt_buy_madetest.json"),
    }
    logs = _fixture("getlogs_window.json")
    blocks = _fixture("block_headers.json")
    tokens = _fixture("token_metadata.json")
    balances = _fixture("balance_of_fsd.json")

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if seen is not None:
            seen.append({"method": body["method"], "params": body["params"], "headers": dict(request.headers)})
        method = body["method"]
        if method == "eth_blockNumber":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "0x34dd740"})
        if method == "eth_getLogs":
            payload = body["params"][0]
            side = "from_side" if len(payload["topics"]) == 2 else "to_side"
            return httpx.Response(200, json=logs[side])
        if method == "eth_getTransactionReceipt":
            answer = receipts.get(str(body["params"][0]).lower())
            return httpx.Response(200, json=answer or {"jsonrpc": "2.0", "id": 1, "result": None})
        if method == "eth_getBlockByNumber":
            header = blocks.get(str(body["params"][0]))
            if header is None:
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": None})
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": header})
        if method == "eth_call":
            call = body["params"][0]
            if str(call["data"]).startswith(_BALANCE_OF):
                # A historical state read. The public node answers it inside its ~10-minute window and
                # refuses it outside, which is exactly the two branches the exit rule has to handle.
                block = str(body["params"][1])
                return httpx.Response(200, json=balances["result" if block == FSD_BALANCE_BLOCK else "pruned"])
            entry = tokens.get(str(call["to"]).lower())
            if entry is None:
                return httpx.Response(
                    200,
                    json={"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "execution reverted"}},
                )
            label = "symbol" if call["data"] == "0x95d89b41" else "decimals"
            return httpx.Response(200, json=entry[label])
        raise AssertionError(f"unexpected method {method}")

    return httpx.MockTransport(handler)


def _chain(seen: list[dict[str, Any]] | None = None) -> RobinhoodChainClient:
    return RobinhoodChainClient(rpc_url="https://rpc.test", transport=_rpc_transport(seen))


async def _with(client: Any, work: Any) -> Any:
    try:
        return await work(client)
    finally:
        await client.aclose()


# --------------------------------------------------------------------------- the chain adapter
def test_the_rpc_adapter_sends_an_explicit_user_agent_on_every_call() -> None:
    """The public endpoint answers 403 to some library defaults; the header is not decoration."""

    seen: list[dict[str, Any]] = []

    async def work(client: RobinhoodChainClient) -> None:
        await client.block_number()

    asyncio.run(_with(_chain(seen), work))

    assert seen[0]["headers"]["user-agent"] == CHAIN_RPC_USER_AGENT


def test_the_recorded_log_window_decodes_into_the_wallets_own_transfer() -> None:
    """The topic array is the whole filter: no `address` restriction, so the token need not be known."""

    seen: list[dict[str, Any]] = []

    async def work(client: RobinhoodChainClient) -> Any:
        return await client.logs(
            from_block=55_432_960,
            to_block=55_433_024,
            topics=[TRANSFER_TOPIC, [address_topic(SELL_WALLET)]],
        )

    logs = asyncio.run(_with(_chain(seen), work))

    assert len(logs) == 1
    assert logs[0].transaction_hash == SELL_TX
    assert logs[0].log_index == 6
    assert logs[0].block_number == 55_432_994
    assert logs[0].address == FSD
    assert seen[0]["params"][0]["fromBlock"] == "0x34dd700"
    assert "address" not in seen[0]["params"][0]


def test_a_receipt_decodes_with_every_log_the_route_emitted() -> None:
    async def work(client: RobinhoodChainClient) -> Any:
        return await client.receipt(SELL_TX)

    receipt = asyncio.run(_with(_chain(), work))

    assert receipt is not None
    assert receipt.status == 1
    assert receipt.block_number == 55_432_994
    assert receipt.transaction_index == 7
    assert len(receipt.logs) == 50


def test_an_unknown_transaction_is_an_answer_and_not_an_error() -> None:
    async def work(client: RobinhoodChainClient) -> Any:
        return await client.receipt("0x" + "1" * 64)

    assert asyncio.run(_with(_chain(), work)) is None


def test_the_block_header_dates_the_event_and_is_read_once() -> None:
    """A log's own `blockTimestamp` is `0x0` on this endpoint, so the header is the only source."""

    seen: list[dict[str, Any]] = []

    async def work(client: RobinhoodChainClient) -> tuple[int, int]:
        return await client.block_timestamp_ms(55_432_994), await client.block_timestamp_ms(55_432_994)

    first, second = asyncio.run(_with(_chain(seen), work))

    assert first == second == 1_788_642_791_000
    assert [call["method"] for call in seen] == ["eth_getBlockByNumber"]


def test_token_metadata_is_decoded_from_the_recorded_calls_and_cached() -> None:
    """The pinned cash token answers `USDG` with six decimals -- read from chain, not assumed."""

    seen: list[dict[str, Any]] = []

    async def work(client: RobinhoodChainClient) -> Any:
        return await client.token(STABLE), await client.token(STABLE), await client.token(FSD)

    stable, again, fsd = asyncio.run(_with(_chain(seen), work))

    assert (stable.symbol, stable.decimals) == ("USDG", 6)
    assert again is stable
    assert (fsd.symbol, fsd.decimals) == ("FSD", 18)
    assert [call["method"] for call in seen].count("eth_call") == 4


def test_a_contract_that_reverts_is_a_token_with_no_readable_metadata() -> None:
    async def work(client: RobinhoodChainClient) -> Any:
        return await client.token("0x" + "ab" * 20)

    token = asyncio.run(_with(_chain(), work))

    assert (token.symbol, token.decimals) == (None, None)


def test_the_recorded_balance_at_the_block_before_the_sell_is_the_exit_denominator() -> None:
    """#572 §3.3's measured case: the wallet held exactly what it sold, so the sample is a 100% exit.

    The number is the recorded `eth_call` answer, as a raw integer in the token's own units -- the same
    integer the ratio is computed from, never a float of it.
    """

    async def work(client: RobinhoodChainClient) -> Any:
        return await client.balance_of(FSD, SELL_WALLET, block_number=55_432_993)

    held = asyncio.run(_with(_chain(), work))

    assert held == 9_412_641_983_109_562_000_000_000
    assert ratio_bps(balance_before_raw=held, quantity_raw=held) == 10_000


def test_a_block_outside_the_nodes_state_window_is_unknown_rather_than_zero() -> None:
    """`-32000 metadata is not found` is a fact about the endpoint, and it is not a balance of nothing.

    This is the branch that makes the whole relaxed verification honest: `None` here is what sends the
    exit rule to the provider's reported bag and puts `site_reported` on the card (#572 決策更新).
    """

    async def work(client: RobinhoodChainClient) -> Any:
        return await client.balance_of(FSD, SELL_WALLET, block_number=55_420_000)

    assert asyncio.run(_with(_chain(), work)) is None


@pytest.mark.parametrize(
    ("status", "code"),
    [(403, "chain_rpc_blocked"), (429, "chain_rpc_rate_limited"), (500, "chain_rpc_http_error")],
)
def test_provider_status_codes_become_one_bounded_vocabulary(status: int, code: str) -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(status, text="no"))

    async def work(client: RobinhoodChainClient) -> None:
        await client.block_number()

    client = RobinhoodChainClient(rpc_url="https://rpc.test", transport=transport)
    with pytest.raises(ChainRpcError) as failure:
        asyncio.run(_with(client, work))

    assert failure.value.code == code
    assert failure.value.status_code == status


def test_an_oversized_answer_is_refused_while_it_is_being_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ceiling has to stop the read. Checking `response.content` measures what already arrived.

    Both endpoints are public and nothing in this repository controls how much they send back, so the
    bound is applied to the declared length first and then to the bytes as they stream in.
    """

    payload = b'{"jsonrpc":"2.0","id":1,"result":"0x1"}' + b" " * 4096
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, content=payload))
    client = RobinhoodChainClient(rpc_url="https://rpc.test", transport=transport)

    async def work(chain: RobinhoodChainClient) -> None:
        await chain.block_number()

    # Under the real ceiling this is an ordinary answer.
    assert asyncio.run(_with(client, work)) is None

    monkeypatch.setattr("tracefold.integrations.robinhood_chain._MAX_BYTES", 64)
    client = RobinhoodChainClient(rpc_url="https://rpc.test", transport=transport)
    with pytest.raises(ChainRpcError) as failure:
        asyncio.run(_with(client, work))

    assert failure.value.code == "chain_rpc_payload_too_large"


def test_a_body_that_declares_itself_too_large_is_refused_before_it_is_read() -> None:
    from tracefold.integrations.http_bounds import ResponseTooLarge, refuse_declared_length

    response = httpx.Response(200, headers={"content-length": "999999"}, content=b"")
    with pytest.raises(ResponseTooLarge):
        refuse_declared_length(response, max_bytes=1024)

    # A chunked answer declares no length; the streaming bound is what covers it.
    refuse_declared_length(httpx.Response(200, content=b"{}"), max_bytes=1024)


def test_an_oversized_roster_answer_is_refused_the_same_way(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, content=b"[]" + b" " * 4096))

    async def work(client: RobinhoodTrenchesClient) -> Any:
        return await client.traders()

    monkeypatch.setattr("tracefold.integrations.robinhoodtrenches._MAX_BYTES", 64)
    client = RobinhoodTrenchesClient(base_url="https://trenches.test", transport=transport, pace_seconds=0.0)
    with pytest.raises(RosterProviderError) as failure:
        asyncio.run(_with(client, work))

    assert failure.value.code == "roster_payload_too_large"


def test_the_chain_identity_is_carried_by_the_adapter() -> None:
    assert _chain().chain_id == ROBINHOOD_CHAIN_ID == 4663


# --------------------------------------------------------------------------- the roster adapter
def _roster_transport(seen: list[httpx.Request] | None = None) -> httpx.MockTransport:
    traders = _fixture("traders_window_7d.json")
    stats = _fixture("trader_stats.json")

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        if request.url.path == "/api/traders":
            return httpx.Response(200, json=traders)
        handle = request.url.path.removeprefix("/api/trader/")
        document = stats.get(handle)
        if document is None:
            return httpx.Response(404, json={"error": "not found"})
        return httpx.Response(200, json=document)

    return httpx.MockTransport(handler)


def _roster(seen: list[httpx.Request] | None = None, *, pace_seconds: float = 0.0) -> RobinhoodTrenchesClient:
    return RobinhoodTrenchesClient(
        base_url="https://trenches.test",
        transport=_roster_transport(seen),
        pace_seconds=pace_seconds,
    )


def _site_transport(*, fail_with: int | None = None) -> httpx.MockTransport:
    """Replay the recorded 2026-09-06 answers for the two card-context endpoints."""

    document = _fixture("trader_document.json")
    tokens = _fixture("tokens_marks.json")

    def handler(request: httpx.Request) -> httpx.Response:
        if fail_with is not None:
            return httpx.Response(fail_with)
        if request.url.path == "/api/tokens":
            return httpx.Response(200, json=tokens)
        if request.url.path.startswith("/api/trader/"):
            return httpx.Response(200, json=document)
        raise AssertionError(f"unexpected path {request.url.path}")

    return httpx.MockTransport(handler)


def _site(**kwargs: Any) -> RobinhoodTrenchesClient:
    return RobinhoodTrenchesClient(base_url="https://site.test", pace_seconds=0.0, transport=_site_transport(**kwargs))


def test_the_recorded_trader_document_decodes_into_the_bags_a_card_carries() -> None:
    """The exit rule's fallback denominator and the card's entry price come out of this one answer."""

    async def work(client: RobinhoodTrenchesClient) -> Any:
        return await client.bags("0xVantaa")

    bags = asyncio.run(_with(_site(), work))

    assert len(bags) == 6
    first = bags[0]
    assert first.token == "0x9d7fba82efaa70823e93ada9b35ee4fbe3c61e18"
    assert first.symbol == "LFG"
    assert first.amount == 22_282_890.591637544
    assert first.avg_price == 3.0286641592777497e-05
    assert first.cost_usd == 674.873921
    # The site publishes seconds; every stamp inside Tracefold is milliseconds.
    assert first.opened_at_ms == 1_788_636_462_000


def test_the_recorded_token_list_decodes_into_the_mark_and_the_pool_depth() -> None:
    """A row the site could not price answers `None`, which is not a mark of zero."""

    async def work(client: RobinhoodTrenchesClient) -> Any:
        return await client.marks()

    marks = asyncio.run(_with(_site(), work))

    slink = marks["0xfa89ed9d12bf74add8253ddfaa426c4d8a0fa603"]
    assert (slink.symbol, slink.mark, slink.liquidity) == ("SLINK", 0.0003781, 49973.0)
    unpriced = marks["0x5b234bcf67f6725fbe01b9bfb2bc9db095589c56"]
    assert (unpriced.mark, unpriced.liquidity) == (None, None)


def test_a_site_that_did_not_answer_is_not_a_wallet_that_holds_nothing() -> None:
    """The distinction the exit rule's third tier depends on: a failure raises, it is never `()`.

    Flattening this into an empty tuple is how a rate-limited request during a partial sell becomes a
    full-exit card -- "the site says no position" and "the site said nothing" have to be different
    values for the caller to be able to decline.
    """

    async def work(client: RobinhoodTrenchesClient) -> Any:
        return await client.bags("0xVantaa")

    with pytest.raises(RosterProviderError) as failure:
        asyncio.run(_with(_site(fail_with=429), work))

    assert failure.value.code == "roster_rate_limited"


def test_bags_and_marks_are_cached_so_a_burst_in_one_token_is_one_request() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/api/tokens":
            return httpx.Response(200, json=_fixture("tokens_marks.json"))
        return httpx.Response(200, json=_fixture("trader_document.json"))

    client = RobinhoodTrenchesClient(
        base_url="https://site.test", pace_seconds=0.0, transport=httpx.MockTransport(handler)
    )

    async def work(session: RobinhoodTrenchesClient) -> None:
        for _ in range(3):
            await session.bags("0xVantaa")
            await session.marks()

    asyncio.run(_with(client, work))

    assert calls == ["/api/trader/0xVantaa", "/api/tokens"]


# --------------------------------------------------------------------------- the price feed
def _dexscreener(*, status: int = 200) -> DexScreenerClient:
    document = _fixture("dexscreener_fsd_tokens.json")

    def handler(request: httpx.Request) -> httpx.Response:
        if status != 200:
            return httpx.Response(status)
        assert request.url.path.endswith(f"/latest/dex/tokens/{FSD}")
        return httpx.Response(200, json=document)

    return DexScreenerClient(base_url="https://dex.test", transport=httpx.MockTransport(handler))


def test_the_recorded_dexscreener_answer_is_priced_by_the_deepest_robinhood_pool() -> None:
    """Twenty real pools for one token, spanning 1.088e-4 to 2.94e-27, and depth is what decides.

    This is the recorded 2026-09-06 answer for FSD. The deepest pool holds $78,696.52 and prints
    0.0001088; the thinnest print in the same document is twenty-three orders of magnitude away. Taking
    the first pair, or the highest price, or any pair at all without ranking by depth, answers a number
    that is not the token's price -- and that number would be a card's receipt.
    """

    async def work(client: DexScreenerClient) -> Any:
        return await client.token_price(FSD)

    assert asyncio.run(_with(_dexscreener(), work)) == Decimal("0.0001088")


def test_a_pool_on_another_chain_never_prices_a_robinhood_token() -> None:
    """Same symbol, different chain, far more liquidity: still not this token's price."""

    document = json.loads(json.dumps(_fixture("dexscreener_fsd_tokens.json")))
    document["pairs"].insert(
        0,
        {
            "chainId": "ethereum",
            "pairAddress": "0x" + "ee" * 32,
            "priceUsd": "12.5",
            "liquidity": {"usd": 90_000_000.0},
        },
    )
    client = DexScreenerClient(
        base_url="https://dex.test",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=document)),
    )

    async def work(session: DexScreenerClient) -> Any:
        return await session.token_price(FSD)

    assert asyncio.run(_with(client, work)) == Decimal("0.0001088")


def test_a_token_dexscreener_does_not_index_is_an_answer_and_not_a_failure() -> None:
    client = DexScreenerClient(
        base_url="https://dex.test",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"pairs": None})),
    )

    async def work(session: DexScreenerClient) -> Any:
        return await session.token_price(FSD)

    assert asyncio.run(_with(client, work)) is None


def test_the_price_feed_has_the_same_bounded_failure_vocabulary_as_the_others() -> None:
    async def work(client: DexScreenerClient) -> Any:
        return await client.token_price(FSD)

    with pytest.raises(DexScreenerError) as failure:
        asyncio.run(_with(_dexscreener(status=429), work))

    assert failure.value.code == "dexscreener_rate_limited"


def test_the_tracked_list_decodes_into_the_fields_the_roster_rules_read() -> None:
    seen: list[httpx.Request] = []

    async def work(client: RobinhoodTrenchesClient) -> Any:
        return await client.traders()

    rows = asyncio.run(_with(_roster(seen), work))

    assert len(rows) == 10
    frank = next(row for row in rows if row.handle == "frankdegods")
    assert frank.address == "0x696d1265c8fc4f14797abebfae3c43ebfa9d8e28"
    assert (frank.closed_trades, frank.followers) == (50, 244_322)
    assert round(frank.open_cost) == 545_894
    assert dict(seen[0].url.params) == {"window": "7d", "stocks": "false"}


def test_a_trader_document_carries_the_profit_factor_no_other_endpoint_publishes() -> None:
    """And carries different closed-trade and P&L numbers than the list does for the same handle.

    The recorded pair says 51 closes and 51,334 realized here against 50 and 510,047 on the list. The
    two endpoints do not agree, which is exactly why the roster stores the list's figures and takes only
    `profit_factor` -- which exists nowhere else -- from this one. Mixing them would put two
    incomparable numbers in one row (#572 §3.1).
    """

    async def work(client: RobinhoodTrenchesClient) -> Any:
        return await client.trader("frankdegods")

    stats = asyncio.run(_with(_roster(), work))

    assert stats is not None
    assert stats.profit_factor is not None
    assert round(stats.profit_factor, 4) == 1.5653
    assert (stats.closed_trades, round(stats.realized_pnl)) == (51, 51_334)


def test_an_unknown_handle_is_absent_rather_than_a_failure() -> None:
    async def work(client: RobinhoodTrenchesClient) -> Any:
        return await client.trader("nobody")

    assert asyncio.run(_with(_roster(), work)) is None


def test_calls_are_paced_apart_because_this_is_somebody_elses_small_site() -> None:
    async def work(client: RobinhoodTrenchesClient) -> float:
        started = time.monotonic()
        await client.traders()
        await client.trader("frankdegods")
        await client.trader("rasmr")
        return time.monotonic() - started

    elapsed = asyncio.run(_with(_roster(pace_seconds=0.05), work))

    assert elapsed >= 0.1


def test_a_list_that_parses_to_nothing_is_a_broken_answer_not_an_empty_roster() -> None:
    """Otherwise one bad deploy at the provider would silently unfollow every wallet."""

    transport = httpx.MockTransport(lambda _request: httpx.Response(200, json=[{"nope": 1}]))

    async def work(client: RobinhoodTrenchesClient) -> Any:
        return await client.traders()

    client = RobinhoodTrenchesClient(base_url="https://trenches.test", transport=transport, pace_seconds=0.0)
    with pytest.raises(RosterProviderError) as failure:
        asyncio.run(_with(client, work))

    assert failure.value.code == "roster_payload_empty"


def test_a_blocked_roster_provider_has_its_own_stable_code() -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(403, text="no"))

    async def work(client: RobinhoodTrenchesClient) -> Any:
        return await client.traders()

    client = RobinhoodTrenchesClient(base_url="https://trenches.test", transport=transport, pace_seconds=0.0)
    with pytest.raises(RosterProviderError) as failure:
        asyncio.run(_with(client, work))

    assert failure.value.code == "roster_blocked"
