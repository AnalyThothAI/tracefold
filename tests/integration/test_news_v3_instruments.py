"""Instrument universe against real PostgreSQL (#75): snapshot idempotence, diff, seeding, alias learning."""

from __future__ import annotations

import pytest

from tests.postgres_test_utils import connect_postgres_test
from tests.postgres_test_utils import reset_postgres_schema as migrate
from tracefold.app.repositories import repositories_for_connection
from tracefold.news.instruments import Instrument, classify

pytestmark = pytest.mark.integration

NOW = 1_787_000_000_000


@pytest.fixture(scope="module")
def conn():
    connection = connect_postgres_test(read_only=False)
    migrate(connection)
    yield connection
    connection.close()


@pytest.fixture(autouse=True)
def _clean(conn):
    conn.execute("DELETE FROM news_market_instruments")
    conn.execute("DELETE FROM news_symbol_aliases")
    conn.commit()


def _inst(venue: str, venue_symbol: str, base: str, quote: str | None = None) -> Instrument:
    return Instrument(
        venue=venue,
        venue_symbol=venue_symbol,
        base_symbol=base,
        instrument_class=classify(base, venue=venue),
        quote_asset=quote,
    )


def test_first_snapshot_seeds_and_does_not_report_listings(conn) -> None:
    repos = repositories_for_connection(conn)
    universe = [
        _inst("binance.perp", "BTCUSDT", "BTC", "USDT"),
        _inst("binance.perp", "UNITREEUSDT", "UNITREE", "USDT"),
        _inst("hl.xyz", "xyz:MSTR", "MSTR"),
    ]
    with repos.transaction():
        result = repos.instruments.apply_snapshot(universe, now_ms=NOW)

    assert result.total == 3
    assert sorted(result.seeded_venues) == ["binance.perp", "hl.xyz"]
    # Every row is new, but a first snapshot is a seed — reporting 3 "listings" would be a lie.
    assert len(result.diff.listed) == 3
    assert result.reportable.empty

    summary = repos.instruments.universe_summary()
    assert summary["trading"] == 3 and summary["base_symbols"] == 3
    assert summary["by_venue"] == {"binance.perp": 2, "hl.xyz": 1}


def test_second_snapshot_reports_only_real_changes_and_is_idempotent(conn) -> None:
    repos = repositories_for_connection(conn)
    first = [_inst("binance.perp", "BTCUSDT", "BTC", "USDT"), _inst("binance.perp", "OLDUSDT", "OLD", "USDT")]
    with repos.transaction():
        repos.instruments.apply_snapshot(first, now_ms=NOW)

    second = [_inst("binance.perp", "BTCUSDT", "BTC", "USDT"), _inst("binance.perp", "ADIUSDT", "ADI", "USDT")]
    with repos.transaction():
        result = repos.instruments.apply_snapshot(second, now_ms=NOW + 3600_000)

    assert result.seeded_venues == ()
    reportable = result.reportable
    assert [i.venue_symbol for i in reportable.listed] == ["ADIUSDT"]
    assert [i.venue_symbol for i in reportable.delisted] == ["OLDUSDT"]
    assert reportable.unchanged == 1

    # first_seen_ms is the listing time and never moves; re-running the same snapshot changes nothing.
    with repos.transaction():
        repeat = repos.instruments.apply_snapshot(second, now_ms=NOW + 7200_000)
    assert repeat.reportable.empty
    listings = repos.instruments.recent_listings(since_ms=NOW + 1, limit=10)
    assert [row["venue_symbol"] for row in listings] == ["ADIUSDT"]
    assert listings[0]["first_seen_ms"] == NOW + 3600_000


def test_a_venue_that_did_not_answer_is_never_read_as_a_mass_delisting(conn) -> None:
    """The failure mode that would matter most: Binance times out and every Binance symbol reads as delisted."""

    repos = repositories_for_connection(conn)
    both = [_inst("binance.perp", "BTCUSDT", "BTC", "USDT"), _inst("hl.perp", "ETH", "ETH")]
    with repos.transaction():
        repos.instruments.apply_snapshot(both, now_ms=NOW)

    # Only Hyperliquid answered this round.
    with repos.transaction():
        result = repos.instruments.apply_snapshot([_inst("hl.perp", "ETH", "ETH")], now_ms=NOW + 3600_000)

    assert result.reportable.empty
    assert repos.instruments.universe_summary()["trading"] == 2
    assert "BTC" in repos.instruments.tradeable_base_symbols()


def test_aliases_seed_and_are_learned_from_the_universe(conn) -> None:
    repos = repositories_for_connection(conn)
    with repos.transaction():
        seeded = repos.instruments.seed_aliases(now_ms=NOW)
        repos.instruments.apply_snapshot(
            [_inst("hl.xyz", "xyz:SKHY", "SKHY"), _inst("hl.xyz", "xyz:SKHX", "SKHX")], now_ms=NOW
        )
        learned = repos.instruments.learn_aliases_from_universe(now_ms=NOW)

    assert seeded > 0 and learned > 0
    aliases = repos.instruments.alias_map()
    # Operator seed: two real contracts, one issuer.
    assert aliases["SKHX"] == "SKHY"
    # Venue-derived: the provider's XYZ- form and the dex-qualified form both resolve.
    assert aliases["XYZ-SKHY"] == "SKHY"
    assert aliases["XYZ:SKHY"] == "SKHY"
    assert repos.instruments.resolve("XYZ-SKHX") == "SKHY"

    # Seeding twice never overwrites: an operator edit survives the next snapshot.
    with repos.transaction():
        conn.execute("UPDATE news_symbol_aliases SET base_symbol = 'CUSTOM' WHERE alias = 'XAU'")
        repos.instruments.seed_aliases(now_ms=NOW + 1)
    assert repos.instruments.alias_map()["XAU"] == "CUSTOM"


def test_tradeable_symbols_include_aliases_only_when_they_resolve(conn) -> None:
    repos = repositories_for_connection(conn)
    with repos.transaction():
        repos.instruments.apply_snapshot([_inst("hl.xyz", "xyz:SKHY", "SKHY")], now_ms=NOW)
        repos.instruments.seed_aliases(now_ms=NOW)

    symbols = repos.instruments.tradeable_base_symbols()
    assert "SKHY" in symbols
    assert "SKHX" in symbols and "SKHYNIX" in symbols  # aliases of a listed issuer
    assert "GOLD" not in symbols  # XAU->GOLD is seeded, but GOLD is not in this universe
    assert "XAU" not in symbols


def test_instrument_class_prefers_the_specific_venue(conn) -> None:
    repos = repositories_for_connection(conn)
    with repos.transaction():
        repos.instruments.apply_snapshot(
            [_inst("binance.perp", "MSTRUSDT", "MSTR", "USDT"), _inst("hl.xyz", "xyz:MSTR", "MSTR")], now_ms=NOW
        )
    # binance.perp defaults to crypto, hl.xyz says equity: the specific classification must win.
    assert repos.instruments.instrument_classes()["MSTR"] == "equity"
    assert repos.instruments.venues_for("MSTR") == ("binance.perp", "hl.xyz")


def test_both_runtime_roles_have_the_expected_privileges(conn) -> None:
    """The migration adds no explicit grants; it relies on ALTER DEFAULT PRIVILEGES. Verify, don't assume."""

    row = conn.execute(
        """
        SELECT has_table_privilege('tracefold_serve', 'public.news_market_instruments', 'SELECT') AS serve_select,
               has_table_privilege('tracefold_serve', 'public.news_market_instruments', 'INSERT') AS serve_insert,
               has_table_privilege(
                 'tracefold_workers', 'public.news_market_instruments', 'SELECT,INSERT,UPDATE,DELETE'
               ) AS workers_dml,
               has_table_privilege('tracefold_workers', 'public.news_symbol_aliases', 'INSERT') AS workers_alias
        """
    ).fetchone()
    assert row["serve_select"] is True
    assert row["serve_insert"] is False
    assert row["workers_dml"] is True
    assert row["workers_alias"] is True
