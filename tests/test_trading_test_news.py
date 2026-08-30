from __future__ import annotations

import pytest

from tracefold.app.cli.commands.trading_test_news import _test_card, _test_headline, _test_targets
from tracefold.app.workers.wiring.manual_trading import manual_trade_sources_from_development_test_news
from tracefold.app.workers.wiring.onchain_trading import onchain_sources_from_development_test_news

NOW = 1_900_000_000_000
TARGET = "a" * 64


def _row(*, kind: str, targets: list[str]) -> dict[str, object]:
    return {
        "source_id": "11111111-1111-1111-1111-111111111111",
        "delivery_target_sha256": TARGET,
        "test_kind": kind,
        "headline_zh": "[开发测试] 交易交互",
        "direction": "bullish",
        "displayed_targets": targets,
        "source_observed_at_ms": NOW,
    }


def test_test_news_defaults_cover_futures_and_multi_target_onchain_selection() -> None:
    assert _test_targets("futures", "") == ("HYPE",)
    assert _test_targets("onchain", "") == ("BLUECHIP", "COPPERINU")
    assert _test_targets("onchain", "0xB200000000000000000000CFBDF64A8706A94A01") == (
        "0xb200000000000000000000cfbdf64a8706a94a01",
    )


def test_test_news_rejects_cross_lane_or_malformed_targets() -> None:
    with pytest.raises(ValueError, match="telegram_test_news_target_invalid"):
        _test_targets("futures", "0xb200000000000000000000cfbdf64a8706a94a01")
    with pytest.raises(ValueError, match="telegram_test_news_targets_invalid"):
        _test_targets("onchain", "A,B,C,D,E")


def test_test_card_is_visibly_marked_and_uses_only_the_explicit_target_line() -> None:
    card = _test_card(
        kind="onchain",
        headline=_test_headline("onchain", ""),
        targets=("BLUECHIP", "COPPERINU"),
        direction="bullish",
        source_id="11111111-1111-1111-1111-111111111111",
        now_ms=NOW,
    )

    content = card["elements"][0]["content"]
    assert "开发测试消息" in content
    assert "BLUECHIP COPPERINU" in content
    assert "不代表真实新闻或交易建议" in content


def test_expiring_test_projection_maps_to_only_its_own_trading_lane() -> None:
    futures = _row(kind="futures", targets=["HYPE"])
    onchain = _row(kind="onchain", targets=["BLUECHIP", "COPPERINU"])

    manual_sources = manual_trade_sources_from_development_test_news(
        futures,
        message_id=42,
        target_sha256=TARGET,
    )
    onchain_sources = onchain_sources_from_development_test_news(
        onchain,
        message_id=43,
        target_sha256=TARGET,
    )

    assert [(source.base_symbol, source.side.value) for source in manual_sources] == [("HYPE", "long")]
    assert [source.ticker for source in onchain_sources] == ["BLUECHIP", "COPPERINU"]
    assert (
        manual_trade_sources_from_development_test_news(
            onchain,
            message_id=43,
            target_sha256=TARGET,
        )
        == ()
    )
    assert (
        onchain_sources_from_development_test_news(
            futures,
            message_id=42,
            target_sha256=TARGET,
        )
        == ()
    )
