"""The source response is the whole membership contract (#697)."""

import pytest

from tracefold.integrations.robinhoodtrenches import RosterProviderError, parse_roster


@pytest.mark.parametrize("count", [147, 201, 256])
def test_all_valid_source_addresses_are_kept(count: int) -> None:
    rows = [
        {"address": f"0x{i:040x}", "handle": str(i), "closed_trades": 0, "profit_factor": -100}
        for i in range(1, count + 1)
    ]
    assert len(parse_roster(rows)) == count
    assert {m.wallet for m in parse_roster(rows)} == {r["address"] for r in rows}


def test_duplicates_are_normalized_with_a_stable_alias() -> None:
    address = "0x" + "ab" * 20
    rows = [{"address": address.upper(), "handle": "z"}, {"address": address, "handle": "a"}]
    assert parse_roster(rows) == parse_roster(list(reversed(rows)))
    assert [(m.wallet, m.handle) for m in parse_roster(rows)] == [(address, "a")]


@pytest.mark.parametrize("bad", [None, [], {}, [{"address": "0x" + "g" * 40}], [None]])
def test_invalid_response_is_not_published_as_partial_membership(bad: object) -> None:
    with pytest.raises(RosterProviderError):
        parse_roster(bad)
    if isinstance(bad, list) and bad:
        with pytest.raises(RosterProviderError):
            parse_roster([{"address": "0x" + "a" * 40}, *bad])


def test_optional_statistics_and_handle_cannot_invalidate_an_address() -> None:
    row = {"address": "0x" + "a" * 40, "handle": None, "realized_pnl": "broken"}
    assert parse_roster([row])[0].handle == ""


@pytest.mark.parametrize("key", ["min_closed_trades", "min_profit_factor", "top_quality", "top_whale_by_open_cost"])
def test_retired_ranking_configuration_is_rejected_not_silently_ignored(key: str) -> None:
    from pydantic import ValidationError

    from tracefold.platform.config.models import Settings

    with pytest.raises(ValidationError, match=key):
        Settings.model_validate({"news": {"chain_tape": {"roster": {key: 20}}}})
