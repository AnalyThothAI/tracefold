"""What `/api/news/market` reads: collapsed runs, per-kind intake, and one Item in full (#553).

Collapsing is *consecutive observations of the same group*, not "the latest row per symbol". The
difference is the whole point: a uniform latest-per-symbol would let one account's Close bury another
account's Open and let a Binance liquidation bury an OKX one, and it would report a group as a single
observation when the group is a run of them.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.postgres_test_utils import connect_postgres_test
from tracefold.app.repository_session import repositories_for_connection
from tracefold.news.liquidations import parse_liquidation
from tracefold.news.market_contracts import (
    REASON_HISTORICAL,
    REASON_MERGING,
    REASON_UNPROCESSED,
    REASON_UNSTRUCTURED,
)
from tracefold.news.market_observations import MarketObservation
from tracefold.news.oi_signals import measurement_definition, oi_source_contract
from tracefold.news.smart_money import parse_smart_money
from tracefold.news.source_contracts import MARKET_KINDS, MARKET_PROVIDER
from tracefold.news.storage import market as market_storage

pytestmark = pytest.mark.integration

NOW = 1_900_000_000_000
OPEN_CURSOR = 1 << 62


@pytest.fixture()
def conn(postgres_clone_dsn: str):
    connection = connect_postgres_test(read_only=False)
    yield connection
    connection.close()


def _item(
    news: Any,
    item_id: str,
    *,
    kind: str,
    at_ms: int,
    parsed: bool = True,
    params: dict | None = None,
    ingest_mode: str = "live",
    strategy_id: str = "1019",
    parse_error: str = "unknown_market_source",
) -> None:
    value = MarketObservation(
        observation_id=item_id,
        kind=kind,
        source_id="opennews",
        source_item_key=item_id,
        source_strategy_id=strategy_id,
        title=item_id,
        raw_first_line=item_id,
        description="",
        event_at_ms=at_ms,
        received_at_ms=at_ms,
        provider_metadata={},
        provider_params=params or {},
        ingest_mode=ingest_mode,
        parse_status="parsed" if parsed else "raw",
        parse_error=None if parsed else parse_error,
    )
    if not hasattr(news, "fixture_observations"):
        news.fixture_observations = {}
    news.fixture_observations[item_id] = value
    if not parsed or kind == "unknown_market":
        news.insert_market_observation(value, now_ms=at_ms)


def _oi(news: Any, item_id: str, *, venue: str, symbol: str, at_ms: int) -> None:
    source = oi_source_contract({"strategies": [{"id": "1019"}]})
    assert source is not None
    base = news.fixture_observations[item_id]
    news.insert_market_observation(
        base.model_copy(
            update=dict(
                oi_event_id=f"event-{item_id}",
                parser_version="oi_signal_v1",
                symbol=symbol,
                raw_instrument=symbol,
                direction="rise",
                oi_change_bps=455,
                oi_value_usd=32170000,
                whale_long_profit_bps=8021,
                whale_oi_ratio_bps=10071,
                provider=MARKET_PROVIDER,
                source_venue=venue,
                source_contract_version=source.contract_version,
                measurement_window_ms=source.measurement_window_ms,
                measurement_definition=measurement_definition(source),
                available_at_ms=at_ms,
            )
        ),
        now_ms=at_ms,
    )


def _typed(news: Any, fact: Any, *, at_ms: int) -> None:
    base = news.fixture_observations[fact.item_id]
    fields = {name: getattr(fact, name) for name in MarketObservation.model_fields if hasattr(fact, name)}
    fields.update(provider=MARKET_PROVIDER, available_at_ms=at_ms)
    if base.kind == "smart_money":
        fields["notional_usd"] = fact.reported_notional_usd
    # Source Item clocks are what the public reader has always used.
    fields.update(event_at_ms=base.event_at_ms, received_at_ms=base.received_at_ms)
    news.insert_market_observation(base.model_copy(update=fields), now_ms=at_ms)


def _groups(news: Any, *, kinds: tuple[str, ...] = (), limit: int = 50) -> list[dict[str, Any]]:
    return _page(news, kinds=kinds, limit=limit)[0]


def _page(news: Any, *, kinds: tuple[str, ...] = (), limit: int = 50) -> tuple[list[dict[str, Any]], bool]:
    return news.market_groups(
        kinds=kinds,
        from_ms=NOW - 3_600_000,
        to_ms=NOW + 3_600_000,
        cursor_received_at_ms=OPEN_CURSOR,
        cursor_item_id="",
        limit=limit,
    )


def test_a_run_of_one_group_collapses_and_a_different_group_between_them_splits_it(conn) -> None:
    """Three BTC observations with one ETH observation in the middle are three groups, not two."""

    repos = repositories_for_connection(conn)
    news = repos.news
    with repos.transaction():
        for offset, (item_id, symbol) in enumerate(
            (("oi-btc-1", "BTC"), ("oi-btc-2", "BTC"), ("oi-eth-1", "ETH"), ("oi-btc-3", "BTC"))
        ):
            at_ms = NOW + offset
            _item(news, item_id, kind="oi", at_ms=at_ms)
            _oi(news, item_id, venue="binance", symbol=symbol, at_ms=at_ms)

    groups = _groups(news)

    # Newest first: the lone BTC after the ETH break, then ETH, then the run of two BTC.
    assert [(group["latest"]["item_id"], group["observation_count"]) for group in groups] == [
        ("oi-btc-3", 1),
        ("oi-eth-1", 1),
        ("oi-btc-2", 2),
    ]
    run = groups[-1]
    assert run["first_event_at_ms"] == NOW
    assert run["last_event_at_ms"] == NOW + 1
    assert run["latest"]["symbol"] == "BTC"


def test_two_venues_reporting_one_instrument_are_two_groups(conn) -> None:
    """A group is provider, venue, native instrument and measurement definition -- not a symbol."""

    repos = repositories_for_connection(conn)
    news = repos.news
    with repos.transaction():
        for offset, (item_id, venue) in enumerate((("oi-venue-a", "binance"), ("oi-venue-b", "okx"))):
            at_ms = NOW + offset
            _item(news, item_id, kind="oi", at_ms=at_ms)
            _oi(news, item_id, venue=venue, symbol="BTC", at_ms=at_ms)

    groups = _groups(news)

    assert [group["latest"]["source_venue"] for group in groups] == ["okx", "binance"]
    assert len({group["group_key"] for group in groups}) == 2


def test_every_group_reports_parse_and_notification_state_as_two_independent_pairs(conn) -> None:
    """With no loop turn taken, the pair names the rule holding the observation, not a send outcome.

    Both records were admitted live, so both are on the notification to-do list. That is what the
    reader is told -- `unprocessed`, awaiting the loop -- rather than a status implying some decision
    about them was already made.
    """

    repos = repositories_for_connection(conn)
    news = repos.news
    with repos.transaction():
        _item(news, "oi-parsed", kind="oi", at_ms=NOW)
        _oi(news, "oi-parsed", venue="binance", symbol="BTC", at_ms=NOW)
        _item(news, "raw-unknown", kind="unknown_market", at_ms=NOW + 1, parsed=False)
        _item(news, "oi-recovered", kind="oi", at_ms=NOW + 2, ingest_mode="recovery")
        _oi(news, "oi-recovered", venue="okx", symbol="BTC", at_ms=NOW + 2)

    groups = _groups(news)

    parse_pairs = {
        group["latest"]["item_id"]: (group["latest"]["parse_status"], group["latest"]["parse_error"])
        for group in groups
    }
    notification_pairs = {
        group["latest"]["item_id"]: (group["notification_status"], group["notification_reason"]) for group in groups
    }
    assert parse_pairs == {
        "oi-recovered": ("parsed", None),
        "raw-unknown": ("raw", "unknown_market_source"),
        "oi-parsed": ("parsed", None),
    }
    assert notification_pairs["oi-parsed"] == ("unprocessed", REASON_UNPROCESSED)
    assert notification_pairs["raw-unknown"] == ("unprocessed", REASON_UNPROCESSED)
    # Recovery replays what the provider published while this process was not listening. Alerting on
    # it would interrupt a reader with an observation whose moment has passed, and would make a
    # reconnection look like a market event, so it is history at admission rather than a to-do. Note
    # that its *parse* pair is identical to the live OI record's: neither pair follows the other.
    assert notification_pairs["oi-recovered"] == ("historical", REASON_HISTORICAL)


def test_a_processed_unstructured_record_is_not_alerted_rather_than_merging(conn) -> None:
    """#582 §3.2. The loop groups it, marks it processed, and writes no track for it.

    The page must tell those two apart, and the only thing that distinguishes them is whether a track
    row exists at all: `merging` promises a card that is being prepared, and for an unstructured
    record none ever will be. The contrast is written on one database -- a parsed OI observation whose
    group has a track holding nothing at this instant reads `merging`, and the raw record beside it
    reads `not_alerted`.
    """

    repos = repositories_for_connection(conn)
    news = repos.news
    with repos.transaction():
        _item(news, "raw-withdraw", kind="smart_money", at_ms=NOW, parsed=False, strategy_id="2026")
        news.market_mark_processed(item_ids=["raw-withdraw"], group_key="raw|smart_money|raw-withdraw")
        _item(news, "oi-merging", kind="oi", at_ms=NOW + 1)
        _oi(news, "oi-merging", venue="binance", symbol="BTC", at_ms=NOW + 1)
        oi_group = "oi|opennews|binance|BTC|" + str(
            measurement_definition(oi_source_contract({"strategies": [{"id": "1019"}]}))
        )
        news.market_mark_processed(item_ids=["oi-merging"], group_key=oi_group)
        conn.execute(
            """
            INSERT INTO news_jobs(job_kind,subject_id,state,detail,created_at_ms,updated_at_ms)
            VALUES ('market_notify',%s,'pending',jsonb_build_object('market_kind','oi','family','oi',
              'last_observed_at_ms',%s::bigint,'last_observed_item_id','oi-merging','pending_reason','',
              'round_started_at_ms',%s::bigint),%s,%s)
            """,
            (oi_group, NOW + 1, NOW + 1, NOW, NOW),
        )

    pairs = {
        group["latest"]["item_id"]: (group["notification_status"], group["notification_reason"])
        for group in _groups(news)
    }
    assert pairs == {
        "raw-withdraw": ("not_alerted", REASON_UNSTRUCTURED),
        "oi-merging": ("merging", REASON_MERGING),
    }

    detail = news.market_item(item_id="raw-withdraw")
    assert detail is not None
    assert (detail["notification_status"], detail["notification_reason"]) == ("not_alerted", REASON_UNSTRUCTURED)
    assert detail["notification_delivery"] is None
    assert detail["notify_group_key"] == "raw|smart_money|raw-withdraw"


def test_an_unparsed_record_is_its_own_group_and_never_merges_with_another_unknown(conn) -> None:
    """#553 §4.1. Missing group fields are not a shared `unknown` bucket."""

    repos = repositories_for_connection(conn)
    news = repos.news
    with repos.transaction():
        _item(news, "raw-a", kind="unknown_market", at_ms=NOW, parsed=False)
        _item(news, "raw-b", kind="unknown_market", at_ms=NOW + 1, parsed=False)

    groups = _groups(news)

    assert [group["observation_count"] for group in groups] == [1, 1]
    assert len({group["group_key"] for group in groups}) == 2


def test_the_kind_filter_narrows_and_the_source_summary_always_names_every_kind(conn) -> None:
    repos = repositories_for_connection(conn)
    news = repos.news
    liquidation = parse_liquidation(
        "SOL Large Short Liquidation 202.71K at $137.01",
        item_id="liq-1",
        fact_id="fact-liq-1",
        source_strategy_id="2083",
        provider_source="okx",
        event_at_ms=NOW,
        received_at_ms=NOW,
    )
    assert liquidation is not None
    with repos.transaction():
        _item(news, "oi-1", kind="oi", at_ms=NOW)
        _oi(news, "oi-1", venue="binance", symbol="BTC", at_ms=NOW)
        _item(news, "liq-1", kind="liquidation", at_ms=NOW + 1)
        _typed(news, liquidation, at_ms=NOW)

    assert [group["market_kind"] for group in _groups(news, kinds=("oi",))] == ["oi"]
    assert [group["market_kind"] for group in _groups(news, kinds=("liquidation",))] == ["liquidation"]
    assert [group["market_kind"] for group in _groups(news)] == ["liquidation", "oi"]

    sources = {row["market_kind"]: row for row in news.market_sources(from_ms=NOW - 1, to_ms=NOW + 3_600_000)}
    assert set(sources) == set(MARKET_KINDS)
    assert (sources["oi"]["received"], sources["oi"]["parsed"], sources["oi"]["groups"]) == (1, 1, 1)
    assert sources["smart_money"] == {
        "market_kind": "smart_money",
        "received": 0,
        "parsed": 0,
        "raw": 0,
        "groups": 0,
        "last_received_at_ms": None,
        # #553 PR-2 puts what a reader was told beside what arrived. No kind was notified here, so
        # every receipt count is zero rather than absent.
        "merged": 0,
        "sent": 0,
        "failed": 0,
        "unknown": 0,
        "last_sent_at_ms": None,
        "last_failed_at_ms": None,
        "last_unknown_at_ms": None,
    }


def test_one_item_reads_back_its_stored_payload_and_its_groups_whole_timeline(conn) -> None:
    """The detail is read by Item identity, so a link into an old group still opens."""

    repos = repositories_for_connection(conn)
    news = repos.news
    address = "0x" + "7" * 40
    account = parse_smart_money(
        "js-2 Close Short SOL $482,113.55 , Price $137.01 , PNL -$8,204.10",
        item_id="wallet-1",
        fact_id="fact-wallet-1",
        source_strategy_id="2026",
        provider_source="",
        related_address=address,
        event_at_ms=NOW,
        received_at_ms=NOW,
    )
    assert account is not None
    params = {"relatedAddress": address, "strategy": {"metrics": {"position_value": {"value": 482113.55}}}}
    with repos.transaction():
        _item(news, "wallet-1", kind="smart_money", at_ms=NOW, params=params)
        _typed(news, account, at_ms=NOW)

    detail = news.market_item(item_id="wallet-1")
    assert detail is not None
    assert detail["provider_params"] == params
    assert (detail["action"], detail["position_side"], detail["account_address"]) == ("close", "short", address)
    assert detail["pnl_usd"] == "-8204.10"
    assert (detail["notification_status"], detail["notification_reason"]) == (
        "unprocessed",
        REASON_UNPROCESSED,
    )
    # No card spoke for it, so there is neither a delivery nor anything it covered.
    assert detail["notification_delivery"] is None
    assert detail["notification_covered_item_ids"] == []
    timeline = news.market_group_timeline(group_key=str(detail["group_key"]))
    assert [row["item_id"] for row in timeline] == ["wallet-1"]
    assert news.market_item(item_id="0" * 64) is None


def test_the_page_cursor_walks_groups_without_repeating_or_skipping_one(conn) -> None:
    repos = repositories_for_connection(conn)
    news = repos.news
    with repos.transaction():
        for offset in range(5):
            item_id = f"oi-page-{offset}"
            _item(news, item_id, kind="oi", at_ms=NOW + offset)
            _oi(news, item_id, venue=f"venue-{offset}", symbol="BTC", at_ms=NOW + offset)

    first = _groups(news, limit=2)
    assert [group["latest"]["item_id"] for group in first] == ["oi-page-4", "oi-page-3"]
    second, _ = news.market_groups(
        kinds=(),
        from_ms=NOW - 3_600_000,
        to_ms=NOW + 3_600_000,
        cursor_received_at_ms=int(first[-1]["oldest_received_at_ms"]),
        cursor_item_id=str(first[-1]["oldest_item_id"]),
        limit=2,
    )
    assert [group["latest"]["item_id"] for group in second] == ["oi-page-2", "oi-page-1"]


def test_the_page_scan_bound_is_reported_and_never_ends_pagination_early(conn, monkeypatch) -> None:
    """#553 SHOULD-FIX 3. A bounded scan must bound one page, not the window.

    The first form capped the *window* read, so a busy 168 h window stopped producing groups once the
    cap was reached and `next_cursor` went null with rows still unread — the list simply ended, and
    the per-kind counts reported the ceiling as the provider's number. Now each page scans from the
    cursor down, so the cap can only split one run, and it says so when it does.
    """

    repos = repositories_for_connection(conn)
    news = repos.news
    with repos.transaction():
        for offset in range(6):
            item_id = f"oi-cap-{offset}"
            _item(news, item_id, kind="oi", at_ms=NOW + offset)
            _oi(news, item_id, venue=f"cap-venue-{offset}", symbol="BTC", at_ms=NOW + offset)

    monkeypatch.setattr(market_storage, "MARKET_WINDOW_ROW_CAP", 2)
    monkeypatch.setattr(
        market_storage,
        "MARKET_GROUPS_SQL",
        market_storage.MARKET_GROUPS_SQL.replace(f"LIMIT {market_storage.MARKET_WINDOW_ROW_CAP}", "LIMIT 2"),
    )

    walked: list[str] = []
    cursor_at, cursor_id = OPEN_CURSOR, ""
    truncations: list[bool] = []
    for _ in range(6):
        page, truncated = news.market_groups(
            kinds=(),
            from_ms=NOW - 1,
            to_ms=NOW + 3_600_000,
            cursor_received_at_ms=cursor_at,
            cursor_item_id=cursor_id,
            limit=1,
        )
        if not page:
            break
        truncations.append(truncated)
        walked.append(str(page[0]["latest"]["item_id"]))
        cursor_at = int(page[0]["oldest_received_at_ms"])
        cursor_id = str(page[0]["oldest_item_id"])

    # Every group is reachable one page at a time even though each page scans at most two rows, and
    # each step continued from the previous page's cursor rather than restarting at the window top.
    assert walked == [f"oi-cap-{offset}" for offset in reversed(range(6))]
    assert len(set(walked)) == len(walked), "a truncated scan must not re-emit a group it already paged"
    # Every page but the last filled its two-row scan and says so; the last one had a single row left.
    assert truncations == [True, True, True, True, True, False]
    # The intake summary is not scan-bounded, so its counts stay exact.
    sources = {row["market_kind"]: row for row in news.market_sources(from_ms=NOW - 1, to_ms=NOW + 3_600_000)}
    assert sources["oi"]["received"] == 6


def test_a_run_longer_than_the_page_scan_reports_a_floor_rather_than_a_wrong_total(conn, monkeypatch) -> None:
    """The one thing a bounded page can still get wrong, made visible instead of silent.

    Four consecutive observations of one group, scanned two at a time: the count a reader is shown is
    a floor and `scan_truncated` says so. This is also what makes the bound observable at all -- with
    six *distinct* groups a capped and an uncapped scan return the same rows, so a test built only on
    those would pass against a statement that ignored the cap entirely.
    """

    repos = repositories_for_connection(conn)
    news = repos.news
    with repos.transaction():
        for offset in range(4):
            item_id = f"oi-run-{offset}"
            _item(news, item_id, kind="oi", at_ms=NOW + offset)
            _oi(news, item_id, venue="binance", symbol="RUN", at_ms=NOW + offset)

    whole, whole_truncated = _page(news, limit=5)
    assert [group["observation_count"] for group in whole] == [4]
    assert whole_truncated is False

    monkeypatch.setattr(market_storage, "MARKET_WINDOW_ROW_CAP", 2)
    bounded, bounded_truncated = _page(news, limit=5)

    assert [group["observation_count"] for group in bounded] == [2], "the scan bound must actually bind"
    assert bounded_truncated is True
    # And the rest of the run is still reachable: the cursor continues below the truncated page.
    rest, _ = news.market_groups(
        kinds=(),
        from_ms=NOW - 3_600_000,
        to_ms=NOW + 3_600_000,
        cursor_received_at_ms=int(bounded[0]["oldest_received_at_ms"]),
        cursor_item_id=str(bounded[0]["oldest_item_id"]),
        limit=5,
    )
    assert [group["observation_count"] for group in rest] == [2]
    assert [group["latest"]["item_id"] for group in rest] == ["oi-run-1"]
