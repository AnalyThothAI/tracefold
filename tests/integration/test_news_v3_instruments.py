"""Instrument universe against real PostgreSQL (#75/#89): snapshot reconciliation, seeds, alias learning."""

from __future__ import annotations

import pytest

from tests.postgres_test_utils import connect_postgres_test
from tracefold.app.repository_session import repositories_for_connection
from tracefold.news.market_review.instruments import Instrument, classify

pytestmark = pytest.mark.integration

NOW = 1_787_000_000_000


@pytest.fixture(scope="module")
def conn(postgres_module_clone_dsn: str):
    connection = connect_postgres_test(read_only=False)
    yield connection
    connection.close()


@pytest.fixture(autouse=True)
def _clean(conn):
    conn.execute("DELETE FROM news_market_instrument_listing_events")
    conn.execute("DELETE FROM news_market_instruments")
    conn.execute("DELETE FROM news_market_instrument_snapshot_state")
    conn.execute("DELETE FROM news_symbol_aliases")
    conn.execute("DELETE FROM news_items")
    conn.commit()


def _row_versions(conn) -> str:
    """One fingerprint of every catalogue row's physical version.

    `xmin` is the transaction that wrote the row's current version, so this changes if and only if a row was
    written — including a write that stores the values it already had, which is exactly the invisible cost this
    module measures (#570 A11).
    """

    row = conn.execute(
        "SELECT coalesce(md5(string_agg(venue || venue_symbol || xmin::text, ',' ORDER BY venue, venue_symbol)), '')"
        "    AS fingerprint FROM news_market_instruments"
    ).fetchone()
    return str(row["fingerprint"])


def _tuple_counters(conn) -> tuple[int, int]:
    """`(inserted, updated)` for the catalogue table, as PostgreSQL itself counts them."""

    conn.execute("SELECT pg_stat_force_next_flush()")
    row = conn.execute(
        "SELECT coalesce(n_tup_ins, 0) AS ins, coalesce(n_tup_upd, 0) AS upd"
        "  FROM pg_stat_user_tables WHERE relname = 'news_market_instruments'"
    ).fetchone()
    return (int(row["ins"]), int(row["upd"])) if row else (0, 0)


def _listing_events(conn) -> int:
    return int(conn.execute("SELECT count(*) AS n FROM news_market_instrument_listing_events").fetchone()["n"])


def _snapshot_state(conn) -> dict[str, int]:
    rows = conn.execute("SELECT venue, last_snapshot_ms FROM news_market_instrument_snapshot_state").fetchall()
    return {str(row["venue"]): int(row["last_snapshot_ms"]) for row in rows}


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
    assert result.written == 3  # every contract is new, and each one records its listing event
    assert _snapshot_state(conn) == {"binance.perp": NOW, "hl.xyz": NOW}

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
    assert result.written == 2  # the new ADIUSDT row and the OLDUSDT delisting; BTCUSDT did not move
    summary = repos.instruments.universe_summary()
    assert summary["trading"] == 2 and summary["delisted"] == 1
    assert summary["last_snapshot_ms"] == NOW + 3600_000

    # Re-running an unchanged catalogue writes no row at all: the refresh time is a venue fact, and it is the
    # only thing that moves (#570 A11).
    versions = _row_versions(conn)
    events = _listing_events(conn)
    with repos.transaction():
        repeat = repos.instruments.apply_snapshot(second, now_ms=NOW + 7200_000)
    assert repeat.delisted == 0 and repeat.written == 0
    assert _row_versions(conn) == versions and _listing_events(conn) == events
    assert repos.instruments.universe_summary()["last_snapshot_ms"] == NOW + 7200_000
    assert _snapshot_state(conn) == {"binance.perp": NOW + 7200_000}

    # A contract that comes back reads as trading again rather than staying delisted forever.
    with repos.transaction():
        relist = repos.instruments.apply_snapshot(first, now_ms=NOW + 10800_000)
    assert repos.instruments.venues_for("OLD") == ("binance.perp",)
    assert relist.written == 2  # OLDUSDT relisted, ADIUSDT delisted; BTCUSDT still untouched


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

    with repos.transaction():
        repos.instruments.apply_snapshot(
            [_inst("binance.perp", "OLDUSDT", "NEW", "USDT")],
            now_ms=NOW + 10_800_000,
        )

    assert (
        repos.news.trade_candidate_instrument(
            base_symbol="OLD",
            venues=("binance.perp",),
            observed_at_ms=NOW + 10_800_000,
        )
        == []
    )
    changed = repos.news.trade_candidate_instrument(
        base_symbol="NEW",
        venues=("binance.perp",),
        observed_at_ms=NOW + 10_800_000,
    )
    assert [(row["venue_symbol"], row["base_symbol"]) for row in changed] == [("OLDUSDT", "NEW")]


def test_a_venue_that_did_not_answer_is_never_read_as_a_mass_delisting(conn) -> None:
    """The failure mode that would matter most: Binance times out and every Binance symbol reads as delisted."""

    repos = repositories_for_connection(conn)
    both = [_inst("binance.perp", "BTCUSDT", "BTC", "USDT"), _inst("hl.perp", "ETH", "ETH")]
    with repos.transaction():
        repos.instruments.apply_snapshot(both, now_ms=NOW)

    # Only Hyperliquid answered this round.
    with repos.transaction():
        result = repos.instruments.apply_snapshot([_inst("hl.perp", "ETH", "ETH")], now_ms=NOW + 3600_000)

    assert result.delisted == 0 and result.venues == ("hl.perp",) and result.written == 0
    assert repos.instruments.universe_summary()["trading"] == 2
    assert repos.instruments.venues_for("BTC") == ("binance.perp",)
    # Binance keeps every row it had *and* the last time it actually answered; only Hyperliquid's refresh
    # moved. The summary reports the newest of the two, which is what "last snapshot" has always meant.
    assert _snapshot_state(conn) == {"binance.perp": NOW, "hl.perp": NOW + 3600_000}
    assert repos.instruments.universe_summary()["last_snapshot_ms"] == NOW + 3600_000


def test_a_venue_that_did_not_answer_keeps_its_rows_physically_untouched(conn) -> None:
    """The other half of the same rule: not delisting a silent venue is not enough if we rewrite it anyway."""

    repos = repositories_for_connection(conn)
    both = [_inst("binance.perp", "BTCUSDT", "BTC", "USDT"), _inst("hl.perp", "ETH", "ETH")]
    with repos.transaction():
        repos.instruments.apply_snapshot(both, now_ms=NOW)

    binance_version = conn.execute(
        "SELECT xmin::text AS version FROM news_market_instruments WHERE venue = 'binance.perp'"
    ).fetchone()["version"]
    with repos.transaction():
        repos.instruments.apply_snapshot([_inst("hl.perp", "ETH", "ETH")], now_ms=NOW + 3600_000)

    assert (
        conn.execute(
            "SELECT xmin::text AS version FROM news_market_instruments WHERE venue = 'binance.perp'"
        ).fetchone()["version"]
        == binance_version
    )


def test_an_unchanged_large_catalogue_is_read_and_not_written(conn) -> None:
    """The finding itself (#570 A11): 16 493 live rows, 3 790 237 cumulative updates, 1.82 GB of WAL in
    production, because every six-hourly refresh restamped every row whether or not the venue's catalogue had
    moved. Five thousand rows is enough to make an accidental restamp impossible to miss.

    Three independent witnesses, because "no write" is the claim: what the repository says it wrote, what
    PostgreSQL counted on the table, and whether any row's physical version changed.
    """

    repos = repositories_for_connection(conn)
    catalogue = [_inst("binance.perp", f"BULK{index:05d}USDT", f"BULK{index:05d}", "USDT") for index in range(5_000)]
    with repos.transaction():
        cold = repos.instruments.apply_snapshot(catalogue, now_ms=NOW)
    assert cold.written == 5_000 and _listing_events(conn) == 5_000

    versions = _row_versions(conn)
    inserted, updated = _tuple_counters(conn)
    with repos.transaction():
        repeat = repos.instruments.apply_snapshot(catalogue, now_ms=NOW + 6 * 3600_000)

    assert repeat.total == 5_000 and repeat.delisted == 0 and repeat.written == 0
    assert _tuple_counters(conn) == (inserted, updated)  # PostgreSQL counted no insert and no update
    assert _row_versions(conn) == versions  # and no row holds a new physical version
    assert _listing_events(conn) == 5_000  # a refresh that changed nothing is not a listing event
    # The one fact a refresh always establishes still moves, and the status page still reads it.
    assert _snapshot_state(conn) == {"binance.perp": NOW + 6 * 3600_000}
    assert repos.instruments.universe_summary()["last_snapshot_ms"] == NOW + 6 * 3600_000


def test_one_changed_field_writes_exactly_one_row_and_records_it(conn) -> None:
    """A venue re-declaring one contract's quote asset is a real catalogue change, and the only one worth a write."""

    repos = repositories_for_connection(conn)
    before = [
        _inst("binance.perp", "AAAUSDT", "AAA", "USDT"),
        _inst("binance.perp", "BBBUSDT", "BBB", "USDT"),
        _inst("binance.perp", "CCCUSDT", "CCC", "USDT"),
    ]
    with repos.transaction():
        repos.instruments.apply_snapshot(before, now_ms=NOW)

    unchanged_versions = conn.execute(
        "SELECT md5(string_agg(venue_symbol || xmin::text, ',' ORDER BY venue_symbol)) AS fingerprint"
        "  FROM news_market_instruments WHERE venue_symbol <> 'BBBUSDT'"
    ).fetchone()["fingerprint"]
    after = [
        _inst("binance.perp", "AAAUSDT", "AAA", "USDT"),
        _inst("binance.perp", "BBBUSDT", "BBB", "USDC"),
        _inst("binance.perp", "CCCUSDT", "CCC", "USDT"),
    ]
    with repos.transaction():
        result = repos.instruments.apply_snapshot(after, now_ms=NOW + 3600_000)

    assert result.written == 1 and result.delisted == 0
    assert (
        conn.execute(
            "SELECT md5(string_agg(venue_symbol || xmin::text, ',' ORDER BY venue_symbol)) AS fingerprint"
            "  FROM news_market_instruments WHERE venue_symbol <> 'BBBUSDT'"
        ).fetchone()["fingerprint"]
        == unchanged_versions
    )
    changed = conn.execute(
        "SELECT quote_asset, observed_at_ms FROM news_market_instruments WHERE venue_symbol = 'BBBUSDT'"
    ).fetchone()
    assert changed["quote_asset"] == "USDC" and int(changed["observed_at_ms"]) == NOW + 3600_000
    # The event ledger is the historical record source-time replay reads, and it gained exactly one row.
    events = conn.execute(
        "SELECT venue_symbol, quote_asset, status, observed_at_ms FROM news_market_instrument_listing_events"
        " WHERE observed_at_ms = %s",
        (NOW + 3600_000,),
    ).fetchall()
    assert [(str(row["venue_symbol"]), str(row["quote_asset"]), str(row["status"])) for row in events] == [
        ("BBBUSDT", "USDC", "trading")
    ]


def test_delisting_and_relisting_write_their_own_rows_and_events(conn) -> None:
    """Both catalogue boundaries still write, and each one still records exactly one event."""

    repos = repositories_for_connection(conn)
    listed = [_inst("binance.perp", "KEEPUSDT", "KEEP", "USDT"), _inst("binance.perp", "GONEUSDT", "GONE", "USDT")]
    with repos.transaction():
        repos.instruments.apply_snapshot(listed, now_ms=NOW)
    with repos.transaction():
        delisting = repos.instruments.apply_snapshot(listed[:1], now_ms=NOW + 3600_000)
    with repos.transaction():
        relisting = repos.instruments.apply_snapshot(listed, now_ms=NOW + 7200_000)

    assert (delisting.written, delisting.delisted) == (1, 1)
    assert (relisting.written, relisting.delisted) == (1, 0)
    ledger = conn.execute(
        "SELECT venue_symbol, status, observed_at_ms FROM news_market_instrument_listing_events"
        " ORDER BY observed_at_ms, venue_symbol"
    ).fetchall()
    assert [(str(row["venue_symbol"]), str(row["status"]), int(row["observed_at_ms"])) for row in ledger] == [
        ("GONEUSDT", "trading", NOW),
        ("KEEPUSDT", "trading", NOW),
        ("GONEUSDT", "delisted", NOW + 3600_000),
        ("GONEUSDT", "trading", NOW + 7200_000),
    ]
    row = conn.execute(
        "SELECT status, observed_at_ms FROM news_market_instruments WHERE venue_symbol = 'GONEUSDT'"
    ).fetchone()
    assert str(row["status"]) == "trading" and int(row["observed_at_ms"]) == NOW + 7200_000
    # KEEPUSDT was listed once and never written again, whatever happened around it.
    keep = conn.execute("SELECT observed_at_ms FROM news_market_instruments WHERE venue_symbol = 'KEEPUSDT'").fetchone()
    assert int(keep["observed_at_ms"]) == NOW


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


def test_search_identity_uses_exact_catalog_alias_and_pair_resolution(conn) -> None:
    repos = repositories_for_connection(conn)
    with repos.transaction():
        repos.instruments.apply_snapshot(
            [_inst("binance.perp", "BTCUSDT", "BTC", "USDT")],
            now_ms=NOW,
        )
        repos.instruments.reconcile_seed_aliases(now_ms=NOW, seeds={"XBT": "BTC"})

    for token in ("BTC", "BTCUSDT", "BTC/USDT", "BTC-USDT", "BTC_USDT", "XBT"):
        identity = repos.instruments.search_identity(token)
        assert identity is not None
        assert identity.base_symbol == "BTC"
        assert identity.event_symbols == ("BTC", "XBT")
    assert repos.instruments.search_identity("BTCT") is None
    assert repos.instruments.search_identity("ABTC") is None


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
