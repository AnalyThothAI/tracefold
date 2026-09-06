from __future__ import annotations

import pytest

from tracefold.news.eval.replay import replay_hits


def _hit(
    record_id: int,
    *,
    text: str,
    strategy_id: int,
    strategy_name: str,
    source_type: str,
    engine_type: str,
) -> dict[str, object]:
    return {
        "id": record_id,
        "text": text,
        "source": "binance",
        "engineType": engine_type,
        "ts": f"2026-08-27T00:00:0{record_id}+00:00",
        "strategy": {
            "id": strategy_id,
            "name": strategy_name,
            "sourceType": source_type,
        },
    }


def test_replay_counts_only_editorial_events_and_never_merges_across_event_kind() -> None:
    """#553. A market frame produces no Event, so it is not in the Event denominator.

    Counting it would make a replay report look like the dedupe lane grew, when what actually changed
    is that market observations left it. They are reported separately under their own name.
    """

    shared_title = "BTC market monitor contract update"
    report = replay_hits(
        [
            _hit(
                1,
                text=shared_title,
                strategy_id=2082,
                strategy_name="Organizational Changes",
                source_type="news",
                engine_type="news",
            ),
            _hit(
                2,
                text=shared_title,
                strategy_id=2083,
                strategy_name="Large-scale liquidation",
                source_type="market",
                engine_type="market",
            ),
            _hit(
                3,
                text="BTC OI Rise 4.55%, OI Value 32.17M, Whale Long Profit 80.21%, Whale/OI Ratio 100.71%",
                strategy_id=1019,
                strategy_name="OI Event Monitor",
                source_type="market",
                engine_type="market",
            ),
        ],
        watchlist_symbols=frozenset(),
        instrument_classes=None,
    )

    assert report["counts"]["events"] == 1
    assert report["counts"]["market_observations"] == 2
    assert {(event["event_kind"], event["admission"]) for event in report["events"]} == {("news", "candidate")}


@pytest.mark.parametrize("reverse", [False, True])
def test_replay_dedupes_one_provider_record_within_each_kind_only(reverse: bool) -> None:
    """One record reported under a news Strategy and a market Strategy is one Item and one Event.

    The market tuple leaves the Event lane entirely; the news tuple still opens exactly one Event, and
    a second copy of the same record still collapses.
    """

    title = "BTC OI Rise 4.55%, OI Value 32.17M, Whale Long Profit 80.21%, Whale/OI Ratio 100.71%"
    news = _hit(
        8,
        text=title,
        strategy_id=1018,
        strategy_name="News Score > 70",
        source_type="news",
        engine_type="news",
    )
    oi = _hit(
        8,
        text=title,
        strategy_id=1019,
        strategy_name="OI Event Monitor",
        source_type="market",
        engine_type="market",
    )
    ordered = [oi, news] if reverse else [news, oi]

    report = replay_hits([*ordered, news], watchlist_symbols=frozenset(), instrument_classes=None)

    assert report["counts"]["items"] == 1
    assert report["counts"]["events"] == 1
    assert report["counts"]["market_observations"] == 1
    assert report["counts"]["duplicate_provider_id"] == 1
    assert {event["event_kind"] for event in report["events"]} == {"news"}


@pytest.mark.parametrize("reverse", [False, True])
def test_a_renamed_market_strategy_is_still_a_market_observation_in_either_order(reverse: bool) -> None:
    """Both tuples are market families now, so neither opens an Event whatever order they arrive in."""

    title = "BTC market source contract observation"
    renamed_oi = _hit(
        12,
        text=title,
        strategy_id=1019,
        strategy_name="wrong OI monitor",
        source_type="market",
        engine_type="market",
    )
    liquidation = _hit(
        12,
        text=title,
        strategy_id=2083,
        strategy_name="Large-scale liquidation",
        source_type="market",
        engine_type="market",
    )

    report = replay_hits(
        [liquidation, renamed_oi] if reverse else [renamed_oi, liquidation],
        watchlist_symbols=frozenset(),
        instrument_classes=None,
    )

    assert report["counts"].get("events", 0) == 0
    assert report["counts"]["market_observations"] == 2
    assert report["events"] == []


def test_an_unknown_market_strategy_is_an_observation_rather_than_an_unsupported_event() -> None:
    report = replay_hits(
        [
            _hit(
                15,
                text="Some wallet did something",
                strategy_id=9999,
                strategy_name="Unbound market monitor",
                source_type="wallet",
                engine_type="market",
            )
        ],
        watchlist_symbols=frozenset(),
        instrument_classes=None,
    )

    assert report["counts"].get("events", 0) == 0
    assert report["counts"]["market_observations"] == 1
