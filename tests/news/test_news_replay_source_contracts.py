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


def test_replay_uses_source_contract_admission_and_never_merges_across_event_kind() -> None:
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
    )

    assert report["counts"]["events"] == 3
    assert {(event["event_kind"], event["admission"]) for event in report["events"]} == {
        ("news", "candidate"),
        ("unsupported_market", "unsupported_market_contract"),
        ("oi", "telemetry_deterministic"),
    }


@pytest.mark.parametrize("reverse", [False, True])
def test_replay_dedupes_one_provider_record_within_each_kind_only(reverse: bool) -> None:
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

    report = replay_hits([*ordered, news], watchlist_symbols=frozenset())

    assert report["counts"]["items"] == 1
    assert report["counts"]["events"] == 2
    assert report["counts"]["duplicate_provider_id"] == 1
    assert {event["event_kind"] for event in report["events"]} == {"news", "oi"}


@pytest.mark.parametrize("reverse", [False, True])
def test_replay_reason_never_splits_the_same_provider_fact_and_kind(reverse: bool) -> None:
    title = "BTC market source contract observation"
    drift = _hit(
        12,
        text=title,
        strategy_id=1019,
        strategy_name="wrong OI monitor",
        source_type="market",
        engine_type="market",
    )
    unsupported = _hit(
        12,
        text=title,
        strategy_id=2083,
        strategy_name="Large-scale liquidation",
        source_type="market",
        engine_type="market",
    )

    report = replay_hits(
        [unsupported, drift] if reverse else [drift, unsupported],
        watchlist_symbols=frozenset(),
    )

    assert report["counts"]["events"] == 1
    assert report["counts"]["duplicate_provider_id"] == 1
    assert report["events"][0]["event_kind"] == "unsupported_market"


def test_replay_strict_parser_reason_fences_different_provider_items() -> None:
    title = "BTC Large Short Liquidation 202.71K at $137.01"
    valid = _hit(
        13,
        text=title,
        strategy_id=2000,
        strategy_name="实时清算",
        source_type="market",
        engine_type="market",
    )
    drift = _hit(
        14,
        text=title,
        strategy_id=2000,
        strategy_name="实时清算",
        source_type="market",
        engine_type="market",
    )
    drift["source"] = "aster"

    report = replay_hits([valid, drift], watchlist_symbols=frozenset())

    assert report["counts"]["events"] == 2
    assert {(event["event_kind"], event["source_contract_reason"]) for event in report["events"]} == {
        ("liquidation", None),
        ("liquidation", "source_contract_drift"),
    }
