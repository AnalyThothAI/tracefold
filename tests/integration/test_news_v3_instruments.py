"""Instrument universe against real PostgreSQL (#75/#89): snapshot reconciliation, seeds, alias learning."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

import tracefold
from tests.postgres_test_utils import connect_postgres_test
from tests.postgres_test_utils import reset_postgres_schema as migrate
from tracefold.app.repository_session import repositories_for_connection
from tracefold.news.market_review.instruments import Instrument, classify

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("postgres_dsn")]

NOW = 1_787_000_000_000


@pytest.fixture(scope="module")
def conn():
    connection = connect_postgres_test(read_only=False)
    migrate(connection)
    yield connection
    connection.close()


@pytest.fixture(autouse=True)
def _clean(conn):
    conn.execute("DELETE FROM news_market_instrument_listing_events")
    conn.execute("DELETE FROM news_market_instruments")
    conn.execute("DELETE FROM news_symbol_aliases")
    conn.execute("DELETE FROM news_items")
    conn.commit()


def _inst(venue: str, venue_symbol: str, base: str, quote: str | None = None, cls: str | None = None) -> Instrument:
    return Instrument(
        venue=venue,
        venue_symbol=venue_symbol,
        base_symbol=base,
        instrument_class=cls or classify(base, venue=venue),  # type: ignore[arg-type]
        quote_asset=quote,
    )


def _item(conn, item_id: str, *, coins: str, published_at_ms: int = NOW) -> None:
    conn.execute(
        """
        INSERT INTO news_items (
          item_id, source_id, source_item_key, title, published_at_ms, observed_at_ms,
          provider_metadata, first_ingest_mode, created_at_ms, updated_at_ms
        ) VALUES (%s, 'opennews', %s, 'headline', %s, %s, %s::jsonb, 'live', %s, %s)
        """,
        (item_id, item_id, published_at_ms, published_at_ms, coins, published_at_ms, published_at_ms),
    )


def test_snapshot_writes_the_universe_and_summarizes_it(conn) -> None:
    repos = repositories_for_connection(conn)
    universe = [
        _inst("binance.perp", "BTCUSDT", "BTC", "USDT"),
        _inst("binance.perp", "TENCENTUSDT", "TENCENT", "USDT", cls="equity"),
        _inst("hl.xyz", "xyz:MSTR", "MSTR"),
    ]
    with repos.transaction():
        result = repos.instruments.apply_snapshot(universe, now_ms=NOW)

    assert result.total == 3
    assert result.venues == ("binance.perp", "hl.xyz")
    assert result.delisted == 0

    summary = repos.instruments.universe_summary()
    assert summary["trading"] == 3 and summary["base_symbols"] == 3
    assert summary["by_venue"] == {"binance.perp": 2, "hl.xyz": 1}
    assert summary["by_class"] == {"crypto": 1, "equity": 2}
    # The class Binance declared must survive being written and read back, not fall to the venue default (#89).
    assert repos.instruments.instrument_classes()["TENCENT"] == "equity"


def test_second_snapshot_reconciles_and_is_idempotent(conn) -> None:
    repos = repositories_for_connection(conn)
    first = [_inst("binance.perp", "BTCUSDT", "BTC", "USDT"), _inst("binance.perp", "OLDUSDT", "OLD", "USDT")]
    with repos.transaction():
        repos.instruments.apply_snapshot(first, now_ms=NOW)

    second = [_inst("binance.perp", "BTCUSDT", "BTC", "USDT"), _inst("binance.perp", "ADIUSDT", "ADI", "USDT")]
    with repos.transaction():
        result = repos.instruments.apply_snapshot(second, now_ms=NOW + 3600_000)

    assert result.delisted == 1  # OLDUSDT is gone from a venue that answered
    summary = repos.instruments.universe_summary()
    assert summary["trading"] == 2 and summary["delisted"] == 1
    assert summary["last_snapshot_ms"] == NOW + 3600_000

    # Re-running an unchanged catalogue delists nothing and only moves last_seen_ms.
    with repos.transaction():
        repeat = repos.instruments.apply_snapshot(second, now_ms=NOW + 7200_000)
    assert repeat.delisted == 0
    assert repos.instruments.universe_summary()["last_snapshot_ms"] == NOW + 7200_000

    # A contract that comes back reads as trading again rather than staying delisted forever.
    with repos.transaction():
        repos.instruments.apply_snapshot(first, now_ms=NOW + 10800_000)
    assert repos.instruments.venues_for("OLD") == ("binance.perp",)


def test_trade_projection_uses_source_time_listing_intervals_across_relisting(conn) -> None:
    repos = repositories_for_connection(conn)
    with repos.transaction():
        repos.instruments.apply_snapshot(
            [_inst("binance.perp", "OLDUSDT", "OLD", "USDT")],
            now_ms=NOW,
        )
        repos.instruments.apply_snapshot(
            [_inst("binance.perp", "BTCUSDT", "BTC", "USDT")],
            now_ms=NOW + 3_600_000,
        )

    assert repos.news.trade_candidate_instrument(base_symbol="OLD", venues=("binance.perp",)) == []
    historical = repos.news.trade_candidate_instrument(
        base_symbol="OLD",
        venues=("binance.perp",),
        observed_at_ms=NOW + 1,
    )
    assert [(row["venue_symbol"], row["status"]) for row in historical] == [("OLDUSDT", "trading")]
    assert (
        repos.news.trade_candidate_instrument(
            base_symbol="OLD",
            venues=("binance.perp",),
            observed_at_ms=NOW + 3_600_000,
        )
        == []
    )

    with repos.transaction():
        repos.instruments.apply_snapshot(
            [_inst("binance.perp", "OLDUSDT", "OLD", "USDT")],
            now_ms=NOW + 7_200_000,
        )

    assert repos.news.trade_candidate_instrument(base_symbol="OLD", venues=("binance.perp",))
    assert (
        repos.news.trade_candidate_instrument(
            base_symbol="OLD",
            venues=("binance.perp",),
            observed_at_ms=NOW + 3_600_001,
        )
        == []
    )
    relisted = repos.news.trade_candidate_instrument(
        base_symbol="OLD",
        venues=("binance.perp",),
        observed_at_ms=NOW + 7_200_000,
    )
    assert [(row["venue_symbol"], row["status"]) for row in relisted] == [("OLDUSDT", "trading")]


def test_a_venue_that_did_not_answer_is_never_read_as_a_mass_delisting(conn) -> None:
    """The failure mode that would matter most: Binance times out and every Binance symbol reads as delisted."""

    repos = repositories_for_connection(conn)
    both = [_inst("binance.perp", "BTCUSDT", "BTC", "USDT"), _inst("hl.perp", "ETH", "ETH")]
    with repos.transaction():
        repos.instruments.apply_snapshot(both, now_ms=NOW)

    # Only Hyperliquid answered this round.
    with repos.transaction():
        result = repos.instruments.apply_snapshot([_inst("hl.perp", "ETH", "ETH")], now_ms=NOW + 3600_000)

    assert result.delisted == 0 and result.venues == ("hl.perp",)
    assert repos.instruments.universe_summary()["trading"] == 2
    assert repos.instruments.venues_for("BTC") == ("binance.perp",)


def test_seed_aliases_are_code_owned_and_reconciled(conn) -> None:
    """The old `ON CONFLICT DO NOTHING` made seeds write-once: a corrected seed never reached a deployed DB (#89)."""

    repos = repositories_for_connection(conn)
    with repos.transaction():
        repos.instruments.reconcile_seed_aliases(now_ms=NOW, seeds={"SKHX": "WRONG", "GONE": "SKHY"})
    assert repos.instruments.alias_map()["SKHX"] == "WRONG"

    with repos.transaction():
        counts = repos.instruments.reconcile_seed_aliases(now_ms=NOW + 1, seeds={"SKHX": "SKHY", "NOKIA": "NOK"})

    aliases = repos.instruments.alias_map()
    assert aliases["SKHX"] == "SKHY"  # corrected in place
    assert aliases["NOKIA"] == "NOK"  # added
    assert "GONE" not in aliases  # dropped from the code, dropped from the table
    assert counts == {"written": 2, "removed": 1}


def test_venue_learned_aliases_survive_seed_reconciliation(conn) -> None:
    repos = repositories_for_connection(conn)
    with repos.transaction():
        repos.instruments.apply_snapshot(
            [_inst("hl.xyz", "xyz:SKHY", "SKHY"), _inst("hl.xyz", "xyz:SKHX", "SKHX")], now_ms=NOW
        )
        learned = repos.instruments.learn_aliases_from_universe(now_ms=NOW)
        repos.instruments.reconcile_seed_aliases(now_ms=NOW + 1)

    assert learned > 0
    aliases = repos.instruments.alias_map()
    assert aliases["XYZ-SKHY"] == "SKHY"  # the provider's prefixed form
    assert aliases["XYZ:SKHX"] == "SKHX"  # the dex-qualified venue symbol
    assert repos.instruments.resolve("XYZ-SKHX") == "SKHY"  # venue hop, then the seed
    assert repos.instruments.dangling_seed_aliases() == () or all(
        row["base_symbol"] != "SKHY" for row in repos.instruments.dangling_seed_aliases()
    )


def test_dangling_seed_aliases_are_reported(conn) -> None:
    """`1810.HK -> XIAOMI` pointed at a symbol no venue lists, and nothing said so for a week (#89)."""

    repos = repositories_for_connection(conn)
    with repos.transaction():
        repos.instruments.apply_snapshot([_inst("binance.perp", "HK1810USDT", "HK1810", "USDT")], now_ms=NOW)
        repos.instruments.reconcile_seed_aliases(now_ms=NOW, seeds={"XIAOMI": "HK1810", "1810.HK": "NOWHERE"})

    assert repos.instruments.dangling_seed_aliases() == ({"alias": "1810.HK", "base_symbol": "NOWHERE"},)
    assert repos.instruments.universe_summary()["dangling_aliases"] == 1


def test_dangling_report_is_silent_before_the_first_snapshot(conn) -> None:
    repos = repositories_for_connection(conn)
    with repos.transaction():
        repos.instruments.reconcile_seed_aliases(now_ms=NOW)
    assert repos.instruments.dangling_seed_aliases() == ()


def test_asset_refs_resolve_the_providers_prefixed_form_to_the_listed_contract(conn) -> None:
    """The chip must name what the tag actually is, whichever of the provider's two forms arrived (#87 review).

    OpenNews ships both `UNITREE` and `XYZ-UNITREE` for one instrument. Resolving the raw tag printed
    `hl.xyz:XYZ-UNITREE` on the chip and only worked at all because `learn_aliases_from_universe` happens to
    write an `XYZ-{base}` row — a builder-DEX base without one would have been struck through while
    `grounding_rollup`, which reads the stripped symbol, counted the same Event as grounded.
    """

    repos = repositories_for_connection(conn)
    with repos.transaction():
        repos.instruments.apply_snapshot(
            [_inst("hl.xyz", "xyz:UNITREE", "UNITREE"), _inst("binance.perp", "BTCUSDT", "BTC")], now_ms=NOW
        )

    refs = repos.instruments.asset_refs(["XYZ-UNITREE", "unitree", "BTC", "SPOT"])

    # Keyed by the raw tag the caller passed, so no caller needs normalization knowledge of its own.
    assert (
        refs["XYZ-UNITREE"]
        == refs["unitree"]
        == {
            "symbol": "UNITREE",
            "base_symbol": "UNITREE",
            "venue": "hl.xyz",
            "listed": True,
        }
    )
    assert refs["BTC"]["venue"] == "binance.perp"
    # A tag that names nothing still gets an entry — a lookup gap must never read as a confirmed listing.
    assert refs["SPOT"] == {"symbol": "SPOT", "base_symbol": "SPOT", "venue": None, "listed": False}


def test_normalization_block_only_reports_the_operator_owned_collapse(conn) -> None:
    """Venue-derived aliases are mechanical; surfacing them would fire the block on routine Events.

    The code-owned rows are `source = 'seed'` since #89 — the console asks for those, not for every alias.
    """

    repos = repositories_for_connection(conn)
    with repos.transaction():
        repos.instruments.apply_snapshot([_inst("hl.xyz", "xyz:SKHY", "SKHY")], now_ms=NOW)
        repos.instruments.reconcile_seed_aliases(now_ms=NOW)
        repos.instruments.learn_aliases_from_universe(now_ms=NOW)

    everything = repos.instruments.aliases_by_base(["SKHY"])["SKHY"]["aliases"]
    seeded_only = repos.instruments.aliases_by_base(["SKHY"], sources=("seed",))["SKHY"]["aliases"]

    # The venue rows exist and would pad the group with forms the reader already assumes.
    assert "XYZ-SKHY" in everything
    assert "XYZ-SKHY" not in seeded_only
    # What survives is the collapse the stable storyline identity depends on.
    assert set(seeded_only) == {"SKHY", "SKHX", "SKHYNIX"}


def test_instrument_classes_cover_the_alias_forms_the_provider_sends(conn) -> None:
    repos = repositories_for_connection(conn)
    with repos.transaction():
        repos.instruments.apply_snapshot(
            [_inst("binance.perp", "MSTRUSDT", "MSTR", "USDT"), _inst("hl.xyz", "xyz:MSTR", "MSTR")], now_ms=NOW
        )
        repos.instruments.learn_aliases_from_universe(now_ms=NOW)
        repos.instruments.reconcile_seed_aliases(now_ms=NOW, seeds={"MICROSTRATEGY": "MSTR"})

    classes = repos.instruments.instrument_classes()
    # binance.perp defaults to crypto, hl.xyz says equity: the specific classification must win.
    assert classes["MSTR"] == "equity"
    assert classes["XYZ-MSTR"] == "equity"  # the provider's prefixed form resolves without a second lookup
    assert classes["MICROSTRATEGY"] == "equity"
    assert repos.instruments.venues_for("MSTR") == ("binance.perp", "hl.xyz")


def test_alias_classes_resolve_in_one_hop_regardless_of_row_order(conn) -> None:
    """`SELECT alias, base_symbol` has no ORDER BY, so a two-hop chain must not resolve on some snapshots and not
    on others — one hop, deterministically, or the Gate's asset class flickers between restarts."""

    repos = repositories_for_connection(conn)
    with repos.transaction():
        repos.instruments.apply_snapshot([_inst("hl.xyz", "xyz:MU", "MU")], now_ms=NOW)
        repos.instruments.reconcile_seed_aliases(now_ms=NOW, seeds={"MICRON": "MU", "MICRONTECH": "MICRON"})

    classes = repos.instruments.instrument_classes()
    assert classes["MICRON"] == "equity"
    assert "MICRONTECH" not in classes  # second hop, never resolved either way


def test_unmatched_provider_tags_rank_the_missing_symbols(conn) -> None:
    repos = repositories_for_connection(conn)
    with repos.transaction():
        repos.instruments.apply_snapshot([_inst("hl.xyz", "xyz:MU", "MU")], now_ms=NOW)
        repos.instruments.reconcile_seed_aliases(now_ms=NOW, seeds={"MICRON": "MU"})
        repos.instruments.learn_aliases_from_universe(now_ms=NOW)

    _item(conn, "a", coins='{"coins": [{"symbol": "MU", "score": 80}, {"symbol": "UWMC", "score": 85}]}')
    _item(conn, "b", coins='{"coins": [{"symbol": "XYZ-MU"}, {"symbol": "UWMC", "score": 70}]}')
    _item(conn, "c", coins='{"coins": [{"symbol": "MICRON"}, {"symbol": "OKB", "score": "n/a"}]}')
    _item(conn, "old", coins='{"coins": [{"symbol": "ANCIENT"}]}', published_at_ms=NOW - 86_400_000)
    conn.commit()

    rows = repos.instruments.unmatched_provider_tags(since_ms=NOW - 1, limit=10)
    assert [r["symbol"] for r in rows] == ["UWMC", "OKB"]  # MU resolves three ways; ANCIENT is out of the window
    assert rows[0] == {"symbol": "UWMC", "tags": 2, "max_score": 85.0}
    assert rows[1]["max_score"] is None  # a non-numeric provider score is not a crash


def test_both_runtime_roles_have_the_expected_privileges(conn) -> None:
    """The current universe is mutable by Workers; the replay history is insert-only."""

    row = conn.execute(
        """
        SELECT has_table_privilege('tracefold_serve', 'public.news_market_instruments', 'SELECT') AS serve_select,
               has_table_privilege('tracefold_serve', 'public.news_market_instruments', 'INSERT') AS serve_insert,
               has_table_privilege('tracefold_workers', 'public.news_market_instruments', 'SELECT')
                 AS workers_select,
               has_table_privilege('tracefold_workers', 'public.news_market_instruments', 'INSERT')
                 AS workers_insert,
               has_table_privilege('tracefold_workers', 'public.news_market_instruments', 'UPDATE')
                 AS workers_update,
               has_table_privilege('tracefold_workers', 'public.news_market_instruments', 'DELETE')
                 AS workers_delete,
               has_table_privilege(
                 'tracefold_serve', 'public.news_market_instrument_listing_events', 'SELECT'
               ) AS serve_history_select,
               has_table_privilege(
                 'tracefold_serve', 'public.news_market_instrument_listing_events', 'INSERT'
               ) AS serve_history_insert,
               has_table_privilege(
                 'tracefold_workers', 'public.news_market_instrument_listing_events', 'SELECT'
               ) AS workers_history_select,
               has_table_privilege(
                 'tracefold_workers', 'public.news_market_instrument_listing_events', 'INSERT'
               ) AS workers_history_insert,
               has_table_privilege(
                 'tracefold_workers', 'public.news_market_instrument_listing_events', 'UPDATE'
               ) AS workers_history_update,
               has_table_privilege(
                 'tracefold_workers', 'public.news_market_instrument_listing_events', 'DELETE'
               ) AS workers_history_delete,
               has_table_privilege('tracefold_workers', 'public.news_symbol_aliases', 'INSERT') AS workers_alias
        """
    ).fetchone()
    assert row["serve_select"] is True
    assert row["serve_insert"] is False
    assert all(row[key] is True for key in ("workers_select", "workers_insert", "workers_update", "workers_delete"))
    assert row["serve_history_select"] is True and row["serve_history_insert"] is False
    assert row["workers_history_select"] is True and row["workers_history_insert"] is True
    assert row["workers_history_update"] is False and row["workers_history_delete"] is False
    assert row["workers_alias"] is True


def _with_reference_tier(repos) -> None:
    """One symbol on a real venue that is also a US ticker, and one that only the directory knows."""

    with repos.transaction():
        repos.instruments.apply_snapshot(
            [
                _inst("binance.perp", "ATOMUSDT", "ATOM", "USDT"),
                _inst("us.listed", "ATOM", "ATOM", cls="equity"),
                _inst("us.listed", "UWMC", "UWMC", cls="equity"),
            ],
            now_ms=NOW,
        )


def test_a_traded_venue_always_beats_the_reference_directory(conn) -> None:
    """`ATOM` is Atomera on the NYSE and the Cosmos token on three exchanges. Ranking alone gets this wrong —
    `equity` outranks `crypto` — so the tiers have to be separate (#91)."""

    repos = repositories_for_connection(conn)
    _with_reference_tier(repos)

    classes = repos.instruments.instrument_classes()
    assert classes["ATOM"] == "crypto"  # the venue that lists it describes it
    assert classes["UWMC"] == "equity"  # nothing lists it, so the directory answers
    assert repos.instruments.venues_for("ATOM") == ("binance.perp", "us.listed")
    # `venues_for` still shows the reference row — an operator asking what a symbol *is* wants to see it — so the
    # tradeable question is answered separately rather than left to be guessed from a venue string.
    assert repos.instruments.is_tradeable("ATOM") is True
    assert repos.instruments.is_tradeable("UWMC") is False


def test_asset_refs_never_claim_a_reference_only_symbol_is_listed(conn) -> None:
    """The chip and the funnel it feeds mean "on a venue we poll" — a US ticker with no perp is not that."""

    repos = repositories_for_connection(conn)
    _with_reference_tier(repos)

    refs = repos.instruments.asset_refs(["ATOM", "UWMC"])
    assert refs["ATOM"] == {"symbol": "ATOM", "base_symbol": "ATOM", "venue": "binance.perp", "listed": True}
    assert refs["UWMC"] == {"symbol": "UWMC", "base_symbol": "UWMC", "venue": None, "listed": False}


def test_an_alias_of_a_traded_symbol_beats_a_us_ticker_of_the_same_name(conn) -> None:
    """`BTT` is a NYSE ticker *and* the seed alias of `BTTC`, a Binance token. Folding the directory in before
    aliases resolve handed a BitTorrent headline to the stock branch (#91 review)."""

    repos = repositories_for_connection(conn)
    with repos.transaction():
        repos.instruments.apply_snapshot(
            [
                _inst("binance.spot", "BTTCUSDT", "BTTC", "USDT"),
                _inst("us.listed", "BTT", "BTT", cls="equity"),
            ],
            now_ms=NOW,
        )
        repos.instruments.reconcile_seed_aliases(now_ms=NOW, seeds={"BTT": "BTTC"})

    classes = repos.instruments.instrument_classes()
    assert classes["BTTC"] == "crypto"
    assert classes["BTT"] == "crypto"  # the alias resolves inside the traded tier, before the directory speaks


def test_a_seed_alias_that_only_the_directory_resolves_is_still_dangling(conn) -> None:
    """Every seed exists to collapse contracts into one storyline bucket. `XAU -> GOLD` landing on Barrick Gold's
    NYSE ticker is the same silent dead end the report was written for (#91 review)."""

    repos = repositories_for_connection(conn)
    with repos.transaction():
        repos.instruments.apply_snapshot(
            [_inst("binance.perp", "BTCUSDT", "BTC", "USDT"), _inst("us.listed", "GOLD", "GOLD", cls="equity")],
            now_ms=NOW,
        )
        repos.instruments.reconcile_seed_aliases(now_ms=NOW, seeds={"XAU": "GOLD"})

    assert repos.instruments.dangling_seed_aliases() == ({"alias": "XAU", "base_symbol": "GOLD"},)


def test_universe_summary_keeps_the_reference_tier_out_of_the_traded_counts(conn) -> None:
    """`在交易合约` has to keep meaning contracts: 13k directory rows would turn that figure into a different one."""

    repos = repositories_for_connection(conn)
    _with_reference_tier(repos)

    summary = repos.instruments.universe_summary()
    assert summary["trading"] == 1 and summary["base_symbols"] == 1 and summary["venues"] == 1
    assert summary["by_venue"] == {"binance.perp": 1}
    assert summary["by_class"] == {"crypto": 1}
    assert summary["reference_symbols"] == 2
    assert summary["last_snapshot_ms"] == NOW  # the timestamp still spans every tier


def _migration_0282() -> Any:
    """Load the 0282 revision by path: `alembic/versions` is not a package."""

    path = (
        Path(tracefold.__file__).resolve().parent
        / "platform/postgres/alembic/versions/20260820_0282_instruments_consolidation.py"
    )
    spec = importlib.util.spec_from_file_location("migration_0282", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_0282_rewrites_alias_rows_that_already_exist(conn) -> None:
    """The first deploy of 0282 failed right here, and a from-scratch test database could not see it.

    `UPDATE ... SET source = 'seed'` ran while the deployed CHECK still allowed only `operator`, so every
    database that already held alias rows aborted the migration — which on this stack means serve and workers do
    not start. An empty table made the same statement a no-op in every test. Replay the statements against the
    pre-0282 shape instead, with a row in the table.
    """

    conn.execute("ALTER TABLE news_symbol_aliases DROP CONSTRAINT news_symbol_aliases_source_check")
    conn.execute(
        "ALTER TABLE news_symbol_aliases ADD CONSTRAINT news_symbol_aliases_source_check"
        " CHECK (source IN ('venue', 'opennews_prefix', 'operator'))"
    )
    conn.execute("ALTER TABLE news_market_instruments ADD COLUMN first_seen_ms bigint")
    conn.execute(
        "INSERT INTO news_symbol_aliases (alias, base_symbol, source, updated_at_ms)"
        " VALUES ('XAU', 'GOLD', 'operator', %s)",
        (NOW,),
    )
    conn.commit()

    for statement in _migration_0282().UPGRADE_SQL:
        conn.execute(statement)
    conn.commit()

    assert conn.execute("SELECT source FROM news_symbol_aliases WHERE alias = 'XAU'").fetchone()["source"] == "seed"
    columns = conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'news_market_instruments'"
    ).fetchall()
    assert "first_seen_ms" not in {str(row["column_name"]) for row in columns}
