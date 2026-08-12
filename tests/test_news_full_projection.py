from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

import tracefold.news.projection as projection_module
from tests.support.news import compute_news_public_clusters
from tracefold.news.identity import normalize_story_text
from tracefold.news.projection import (
    NewsProjectionInputExceeded,
    NewsProjectionSnapshot,
    compute_news_story_projection,
)
from tracefold.news.sources import public_rss_sources
from tracefold.news.story_store import (
    NEWS_STORY_INPUT_BYTES_CAP,
    NEWS_STORY_INPUT_ROW_CAP,
    load_story_projection,
)


def _row(
    item_id: str,
    title: str,
    *,
    published_at_ms: int,
    reporting_origin: str,
    source_id: str = "opennews",
    tier: int = 4,
) -> dict[str, object]:
    return {
        "item_id": item_id,
        "source_id": source_id,
        "canonical_url": None,
        "reporting_origin": reporting_origin,
        "title": title,
        "description": "",
        "published_at_ms": published_at_ms,
        "tier": tier,
        "source_kind": "opennews",
        "source_position": None,
        "memberships": (),
    }


def _snapshot(*rows: dict[str, object]) -> NewsProjectionSnapshot:
    return NewsProjectionSnapshot(
        input_fingerprint="a" * 64,
        scoring_epoch_ms=2_000_000_000_000,
        current_input_fingerprint=None,
        rows=rows,
    )


def _rss_row(
    item_id: str,
    *,
    source_index: int,
    source_position: int,
    title: str,
    published_at_ms: int,
) -> dict[str, object]:
    source = public_rss_sources()[source_index]
    return {
        "item_id": item_id,
        "source_id": source.source_id,
        "canonical_url": f"https://example.test/{item_id}",
        "reporting_origin": source.name,
        "title": title,
        "description": "",
        "published_at_ms": published_at_ms,
        "tier": source.tier,
        "source_kind": "rss",
        "source_position": source_position,
        "memberships": source.memberships,
    }


def test_opennews_only_projection_does_not_load_the_rss_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected_catalog_read() -> tuple[object, ...]:
        raise AssertionError("RSS catalog must stay dormant for an OpenNews-only projection")

    monkeypatch.setattr(projection_module, "public_rss_membership_sources", unexpected_catalog_read)

    projection = compute_news_story_projection(
        _snapshot(
            _row("opennews-only", "Central bank holds rates steady", published_at_ms=10, reporting_origin="Reuters")
        )
    )

    assert len(projection["stories"]) == 1


def test_story_id_is_exact_sha256_of_earliest_normalized_title() -> None:
    earliest_title = "Fed holds rates steady"
    projection = compute_news_story_projection(
        _snapshot(
            _row("later", "Fed holds rates steady today", published_at_ms=20, reporting_origin="ap"),
            _row("early", earliest_title, published_at_ms=10, reporting_origin="reuters"),
        )
    )
    assert len(projection["stories"]) == 1
    assert (
        projection["stories"][0]["story_id"]
        == hashlib.sha256(normalize_story_text(earliest_title).encode()).hexdigest()
    )


def test_canonical_utf16_clamp_hashes_like_web_text_encoder() -> None:
    title = "a" * 119 + "𝔸" + "z"
    projection = compute_news_story_projection(
        _snapshot(_row("astral-boundary", title, published_at_ms=10, reporting_origin="reuters"))
    )

    assert projection["stories"][0]["canonical_key"] == hashlib.sha256(("a" * 119 + "\ufffd").encode()).hexdigest()


def test_empty_normalized_titles_use_the_public_per_item_sentinel_identity() -> None:
    projection = compute_news_story_projection(
        _snapshot(
            _row("emoji", "👑👑👑", published_at_ms=10, reporting_origin="aeyakovenko"),
            _row("punctuation", "!!!", published_at_ms=11, reporting_origin="reuters"),
        )
    )

    story_ids = {story["canonical_title"]: story["story_id"] for story in projection["stories"]}
    assert story_ids == {
        "👑👑👑": hashlib.sha256("untrackable:aeyakovenko:👑👑👑:emoji".encode()).hexdigest(),
        "!!!": hashlib.sha256(b"untrackable:reuters:!!!:punctuation").hexdigest(),
    }
    assert len(projection["memberships"]) == 2


def test_lexical_components_remain_distinct_from_public_tracking_hash() -> None:
    projection = compute_news_story_projection(
        _snapshot(
            _row("hyphenated", "Alpha-Beta!!!", published_at_ms=10, reporting_origin="reuters"),
            _row("joined", "AlphaBeta???", published_at_ms=11, reporting_origin="ap"),
            _row("emoji-1", "👑👑👑", published_at_ms=12, reporting_origin="aeyakovenko"),
            _row("emoji-2", "👑👑👑", published_at_ms=13, reporting_origin="aeyakovenko"),
        )
    )

    story_by_id = {story["story_id"]: story for story in projection["stories"]}
    assert set(story_by_id) == {
        hashlib.sha256(b"alpha beta").hexdigest(),
        hashlib.sha256(b"alphabeta").hexdigest(),
        hashlib.sha256("untrackable:aeyakovenko:👑👑👑:emoji-1".encode()).hexdigest(),
        hashlib.sha256("untrackable:aeyakovenko:👑👑👑:emoji-2".encode()).hexdigest(),
    }
    assert {story["canonical_key"] for story in story_by_id.values()} == {
        hashlib.sha256(b"alphabeta").hexdigest(),
        hashlib.sha256("untrackable:aeyakovenko:👑👑👑".encode()).hexdigest(),
    }
    assert all(story["item_count"] == 1 for story in story_by_id.values())
    assert all(story["source_count"] == 1 for story in story_by_id.values())
    assert len(projection["memberships"]) == 4


def test_entity_signal_uses_public_tracking_hash_without_folding_components() -> None:
    now_ms = 2_000_000_000_000
    projection = compute_news_story_projection(
        _snapshot(
            _row("hyphenated", "Iran-talks", published_at_ms=now_ms - 3, reporting_origin="reuters"),
            _row("duplicate", "Iran-talks", published_at_ms=now_ms - 2, reporting_origin="ap"),
            _row("joined", "Irantalks", published_at_ms=now_ms - 1, reporting_origin="bbc"),
        )
    )

    assert len(projection["stories"]) == 2
    joined = next(story for story in projection["stories"] if story["canonical_title"] == "Irantalks")
    assert joined["item_count"] == 1
    assert joined["importance_factors"]["entity_corroboration_boost"] == 8


def test_public_seed_stage_excludes_short_title_without_removing_story_membership() -> None:
    projection = compute_news_story_projection(
        _snapshot(_row("short", "War", published_at_ms=2_000_000_000_000, reporting_origin="reuters"))
    )

    assert len(projection["stories"]) == 1
    assert len(projection["memberships"]) == 1
    assert projection["selection_snapshot"]["top_stories"] == []
    assert projection["selection_snapshot"]["selection_stats"]["considered"] == 0


def test_public_seed_stage_filters_short_members_before_adapting_story_cluster() -> None:
    projection = compute_news_story_projection(
        _snapshot(
            _row(
                "short-tier-one",
                "War attack",
                published_at_ms=2_000_000_000_000,
                reporting_origin="reuters",
            ),
            _row(
                "eligible-tier-four",
                "War attack today",
                published_at_ms=1_999_999_999_999,
                reporting_origin="field-wire",
            ),
        )
    )

    assert projection["stories"][0]["item_count"] == 2
    assert projection["stories"][0]["source_count"] == 2
    assert projection["stories"][0]["representative_title"] == "War attack"
    assert projection["selection_snapshot"]["selection_stats"]["considered"] == 1
    top_story = projection["selection_snapshot"]["top_stories"][0]
    assert {
        key: top_story[key]
        for key in (
            "primary_title",
            "primary_source",
            "source_count",
            "unique_source_count",
            "sources",
            "member_titles",
        )
    } == {
        "primary_title": "War attack today",
        "primary_source": "field-wire",
        "source_count": 1,
        "unique_source_count": 1,
        "sources": ["field-wire"],
        "member_titles": ["War attack today"],
    }


def test_public_seed_stage_reclusters_after_short_bridge_is_removed() -> None:
    projection = compute_news_story_projection(
        _snapshot(
            _row("bridge", "Iran", published_at_ms=1, reporting_origin="reuters"),
            _row("attack", "Iran attack", published_at_ms=2, reporting_origin="ap"),
            _row("crisis", "Iran crisis", published_at_ms=3, reporting_origin="bbc"),
        )
    )

    assert len(projection["stories"]) == 1
    assert projection["stories"][0]["item_count"] == 3
    public_stories = projection["selection_snapshot"]["top_stories"]
    assert [cluster["member_titles"] for cluster in public_stories] == [
        ["Iran attack"],
        ["Iran crisis"],
    ]
    assert {cluster["story_id"] for cluster in public_stories} == {projection["stories"][0]["story_id"]}
    assert projection["selection_snapshot"]["selection_stats"]["considered"] == 2


def test_public_population_caps_each_rss_category_and_joins_opennews_primary_once() -> None:
    sources = public_rss_sources()
    politics_indexes = [index for index, source in enumerate(sources) if source.memberships == ("politics",)][:5]
    assert len(politics_indexes) == 5
    rows = [
        _rss_row(
            f"rss-{index}",
            source_index=politics_indexes[index // 5],
            source_position=index % 5,
            title=f"Public policy report number {index} from capital",
            published_at_ms=2_000_000_000_000 - index,
        )
        for index in range(25)
    ]
    rows.append(
        _row(
            "opennews-primary",
            "OpenNews primary public report",
            published_at_ms=2_000_000_000_000,
            reporting_origin="Independent Wire",
        )
    )

    projection = compute_news_story_projection(_snapshot(*rows))

    assert len(projection["population_item_ids"]) == 21
    assert "opennews-primary" in projection["population_item_ids"]
    assert len({member["item_id"] for member in projection["memberships"]}) == 21


def test_duplicate_rss_memberships_expand_public_count_but_not_physical_story_membership() -> None:
    sources = public_rss_sources()
    duplicate_index = next(index for index, source in enumerate(sources) if len(source.memberships) == 2)
    row = _rss_row(
        "duplicate-membership",
        source_index=duplicate_index,
        source_position=0,
        title="Oil market reports major supply disruption today",
        published_at_ms=2_000_000_000_000,
    )

    snapshot = _snapshot(row)
    projection = compute_news_story_projection(snapshot)
    public_clusters = compute_news_public_clusters(snapshot)

    assert projection["population_item_ids"] == ["duplicate-membership"]
    assert projection["memberships"] == [
        {"story_id": projection["stories"][0]["story_id"], "item_id": "duplicate-membership"}
    ]
    assert len(public_clusters) == 1
    assert public_clusters[0]["source_count"] == 2
    assert public_clusters[0]["unique_source_count"] == 1


def test_source_count_uses_reporting_origin_not_acquisition_source() -> None:
    projection = compute_news_story_projection(
        _snapshot(
            _row(
                "rss",
                "Major earthquake strikes coast",
                published_at_ms=10,
                reporting_origin="reuters",
                source_id="rss",
            ),
            _row("wire", "Major earthquake strikes coast", published_at_ms=11, reporting_origin="reuters"),
            _row("ap", "Major earthquake strikes coast", published_at_ms=12, reporting_origin="ap"),
        )
    )

    assert projection["stories"][0]["item_count"] == 3
    assert projection["stories"][0]["source_count"] == 2
    assert projection["stories"][0]["facet_facts"] == {
        "source_ids": ["opennews", "rss"],
        "reporting_origins": ["ap", "reuters"],
    }
    assert projection["stories"][0]["importance_factors"]["reporting_origin_count"] == 2


def test_story_facet_facts_trim_dedupe_and_sort_raw_labels_before_fingerprinting() -> None:
    base = compute_news_story_projection(
        _snapshot(
            _row(
                "wire-a",
                "Major earthquake strikes coast",
                published_at_ms=10,
                reporting_origin=" Reuters ",
                source_id="opennews",
            ),
            _row(
                "wire-b",
                "Major earthquake strikes coast",
                published_at_ms=11,
                reporting_origin="Reuters",
                source_id="opennews",
            ),
            _row(
                "rss",
                "Major earthquake strikes coast",
                published_at_ms=12,
                reporting_origin="reuters",
                source_id="rss",
            ),
        )
    )["stories"][0]
    changed = compute_news_story_projection(
        _snapshot(
            _row(
                "wire-a",
                "Major earthquake strikes coast",
                published_at_ms=10,
                reporting_origin=" Reuters ",
                source_id="opennews",
            ),
            _row(
                "wire-b",
                "Major earthquake strikes coast",
                published_at_ms=11,
                reporting_origin="Reuters",
                source_id="opennews",
            ),
            _row(
                "rss",
                "Major earthquake strikes coast",
                published_at_ms=12,
                reporting_origin="Associated Press",
                source_id="rss",
            ),
        )
    )["stories"][0]

    assert base["facet_facts"] == {
        "source_ids": ["opennews", "rss"],
        "reporting_origins": ["Reuters", "reuters"],
    }
    assert base["state_fingerprint"] != changed["state_fingerprint"]


def test_public_sources_use_pinned_javascript_locale_order_within_tier() -> None:
    projection = compute_news_story_projection(
        _snapshot(
            *(
                _row(
                    f"source-{index}",
                    "Central bank announces emergency policy meeting",
                    published_at_ms=10 + index,
                    reporting_origin=origin,
                )
                for index, origin in enumerate(("@z", "_a", "zulu", "éclair", "AP", "ap"))
            )
        )
    )

    assert projection["selection_snapshot"]["top_stories"][0]["sources"] == [
        "_a",
        "@z",
        "ap",
        "AP",
        "éclair",
        "zulu",
    ]


def test_public_selector_receives_canonical_component_order_for_full_ties() -> None:
    titles = (
        "Alpine orchard calendar mercury sapphire",
        "Beacon lantern velvet quartz meadow",
        "Copper willow archive juniper mosaic",
        "Delta parchment violet timber harbor",
        "Ember gallery cedar opal compass",
        "Fable granite orchid silver basket",
        "Garnet library maple cobalt window",
        "Hazel museum ribbon amber valley",
        "Ivory notebook tulip bronze garden",
    )
    rows: list[dict[str, object]] = []
    for index, title in enumerate(titles):
        rows.extend(
            (
                _row(
                    f"{index:02d}-a",
                    title,
                    published_at_ms=2_000_000_000_000,
                    reporting_origin=f"primary-{index}",
                ),
                _row(
                    f"{index:02d}-b",
                    title,
                    published_at_ms=2_000_000_000_000,
                    reporting_origin=f"secondary-{index}",
                ),
            )
        )

    projection = compute_news_story_projection(_snapshot(*rows))

    assert [story["primary_title"] for story in projection["selection_snapshot"]["top_stories"]] == list(titles[:8])
    assert projection["selection_snapshot"]["selection_stats"]["overflow_dropped"] == 1


def test_exact_duplicate_hot_bucket_is_one_story_without_private_rules() -> None:
    rows = tuple(
        _row(
            f"item-{index:04d}",
            "Central bank announces emergency policy meeting",
            published_at_ms=1_000 + index,
            reporting_origin=f"origin-{index % 9}",
        )
        for index in range(1_000)
    )

    projection = compute_news_story_projection(_snapshot(*rows))

    assert len(projection["stories"]) == 1
    assert projection["stories"][0]["item_count"] == 1_000
    assert len(projection["memberships"]) == 1_000


def test_absent_historical_bridge_cannot_union_current_components() -> None:
    left = "Turkey hikes rates to 50% in surprise move"
    expired_bridge = "Turkey hikes interest rates to 50% in surprise move"
    right = "Turkey central bank hikes interest rates"

    current = compute_news_story_projection(
        _snapshot(
            _row("left", left, published_at_ms=10, reporting_origin="reuters"),
            _row("right", right, published_at_ms=11, reporting_origin="ap"),
        )
    )
    with_bridge = compute_news_story_projection(
        _snapshot(
            _row("left", left, published_at_ms=10, reporting_origin="reuters"),
            _row("bridge", expired_bridge, published_at_ms=11, reporting_origin="bloomberg"),
            _row("right", right, published_at_ms=12, reporting_origin="ap"),
        )
    )

    assert len(current["stories"]) == 2
    assert len(with_bridge["stories"]) == 1


def test_bounded_story_snapshot_accepts_current_full_window_shape() -> None:
    rows = tuple(
        _row(
            f"item-{index:04d}",
            f"Market update {index} " + ("x" * 380),
            published_at_ms=1_000 + index,
            reporting_origin=f"origin-{index % 9}",
        )
        for index in range(8_000)
    )

    projection_module._require_bounded_snapshot(_snapshot(*rows))


def test_bounded_story_snapshot_rejects_one_row_over_the_hard_cap() -> None:
    rows = tuple(
        _row(
            f"item-{index:05d}",
            f"Market update {index}",
            published_at_ms=1_000 + index,
            reporting_origin="wire",
        )
        for index in range(NEWS_STORY_INPUT_ROW_CAP + 1)
    )

    with pytest.raises(NewsProjectionInputExceeded, match="news_story_input_row_cap"):
        projection_module._require_bounded_snapshot(_snapshot(*rows))


def test_bounded_story_snapshot_accepts_exactly_the_hard_row_cap() -> None:
    rows = tuple(
        _row(
            f"item-{index:05d}",
            "Market update",
            published_at_ms=1_000 + index,
            reporting_origin="wire",
        )
        for index in range(NEWS_STORY_INPUT_ROW_CAP)
    )

    projection_module._require_bounded_snapshot(_snapshot(*rows))


def test_bounded_story_snapshot_rejects_input_over_the_byte_cap() -> None:
    oversized = _row(
        "oversized",
        "x" * NEWS_STORY_INPUT_BYTES_CAP,
        published_at_ms=1_000,
        reporting_origin="wire",
    )

    with pytest.raises(NewsProjectionInputExceeded, match="news_story_input_byte_cap"):
        projection_module._require_bounded_snapshot(_snapshot(oversized))


def test_repository_rebuild_cannot_bypass_the_story_input_cap() -> None:
    with pytest.raises(NewsProjectionInputExceeded, match="news_story_input_row_cap"):
        compute_news_story_projection(
            NewsProjectionSnapshot(
                input_fingerprint="over-cap",
                scoring_epoch_ms=1_000,
                current_input_fingerprint=None,
                rows=tuple(
                    {
                        "item_id": str(index),
                        "source_kind": "opennews",
                    }
                    for index in range(NEWS_STORY_INPUT_ROW_CAP + 1)
                ),
            )
        )


def test_story_load_rejects_the_cap_plus_one_sentinel_before_returning() -> None:
    class _Connection:
        def __init__(self) -> None:
            self.calls = 0

        def execute(self, _query: str, _params: object = None) -> SimpleNamespace:
            self.calls += 1
            if self.calls == 1:
                assert "news_projection_summary" in _query
                return SimpleNamespace(fetchone=lambda: None)
            assert self.calls == 2
            return SimpleNamespace(
                fetchone=lambda: {
                    "item_count": NEWS_STORY_INPUT_ROW_CAP + 1,
                    "minimum_input_bytes": 0,
                }
            )

    conn = _Connection()
    repository = SimpleNamespace(conn=conn, stable_json_hash=lambda _value: "unreachable")

    with pytest.raises(NewsProjectionInputExceeded, match="news_story_input_row_cap"):
        load_story_projection(repository, now_ms=1_000)
    assert conn.calls == 2


def test_story_load_rejects_wide_input_before_fetching_rows() -> None:
    class _Connection:
        def __init__(self) -> None:
            self.calls = 0

        def execute(self, _query: str, _params: object = None) -> SimpleNamespace:
            self.calls += 1
            if self.calls == 1:
                assert "news_projection_summary" in _query
                return SimpleNamespace(fetchone=lambda: None)
            assert self.calls == 2
            return SimpleNamespace(
                fetchone=lambda: {
                    "item_count": 1,
                    "minimum_input_bytes": NEWS_STORY_INPUT_BYTES_CAP + 1,
                }
            )

    conn = _Connection()
    repository = SimpleNamespace(conn=conn, stable_json_hash=lambda _value: "unreachable")

    with pytest.raises(NewsProjectionInputExceeded, match="news_story_input_byte_cap"):
        load_story_projection(repository, now_ms=1_000)
    assert conn.calls == 2
