from __future__ import annotations

import itertools
import json
from pathlib import Path
from typing import Any

import pytest

from tracefold.news.projection import NewsStoryFactSnapshot, build_story_projection
from tracefold.news.sources import public_rss_membership_sources
from tracefold.news.story_store import NewsProjectionInputExceeded

NOW_MS = 2_000_000_000_000
GOLDEN_PATH = Path(__file__).parent / "fixtures" / "news_story_v2_golden.json"


def _row(
    item_id: str,
    title: str,
    *,
    published_at_ms: int,
    provider_identity: tuple[dict[str, str], ...] = (),
) -> dict[str, object]:
    return {
        "item_id": item_id,
        "source_id": "news-opennews",
        "canonical_url": None,
        "reporting_origin": f"wire-{item_id}",
        "title": title,
        "description": "",
        "published_at_ms": published_at_ms,
        "title_fingerprint": item_id.rjust(64, "0")[-64:],
        "tier": 4,
        "source_kind": "opennews",
        "source_position": None,
        "memberships": (),
        "provider_identity": provider_identity,
    }


def _snapshot(*rows: dict[str, object]) -> NewsStoryFactSnapshot:
    return NewsStoryFactSnapshot(
        material_snapshot_fingerprint="a" * 64,
        evaluation_time_ms=NOW_MS,
        published_material_snapshot_fingerprint=None,
        rows=rows,
    )


def test_story_projection_splits_repeated_sec_and_oi_templates_by_strong_event_facts() -> None:
    projection = build_story_projection(
        _snapshot(
            _row(
                "sec-alpha",
                "ALPHA CAPITAL raises share stake in Acme Mining to 8.4% - SEC Filing",
                published_at_ms=NOW_MS - 4_000,
            ),
            _row(
                "sec-beta",
                "BETA CAPITAL raises share stake in Delta Energy to 8.4% - SEC Filing",
                published_at_ms=NOW_MS - 3_000,
            ),
            _row(
                "oi-btc",
                "BTC OI Rise 3.4% OI Value $21B Whale/OI Ratio 1.2",
                published_at_ms=NOW_MS - 2_000,
                provider_identity=({"symbol": "BTC", "market_type": "cex", "match": "BTC"},),
            ),
            _row(
                "oi-eth",
                "ETH OI Rise 3.4% OI Value $21B Whale/OI Ratio 1.2",
                published_at_ms=NOW_MS - 1_000,
                provider_identity=({"symbol": "ETH", "market_type": "cex", "match": "ETH"},),
            ),
        )
    )

    assert len(projection.stories) == 4
    assert {story["item_count"] for story in projection.stories} == {1}
    assert all("identity_evidence" in story for story in projection.stories)


def test_story_projection_preserves_reporting_period_before_numeric_normalization() -> None:
    projection = build_story_projection(
        _snapshot(
            _row(
                "filing-fy2028",
                "Acme reports FY2028 revenue guidance of $21B",
                published_at_ms=NOW_MS - 2_000,
            ),
            _row(
                "filing-fy2029",
                "Acme reports FY2029 revenue guidance of $21B",
                published_at_ms=NOW_MS - 1_000,
            ),
        )
    )

    assert len(projection.stories) == 2
    assert "period_conflict" in {
        reason for story in projection.stories for reason in story["identity_evidence"]["rejection_reasons"]
    }


def test_story_projection_merges_chinese_numeric_and_paraphrase_equivalents() -> None:
    projection = build_story_projection(
        _snapshot(
            _row(
                "world-liberty-simplified",
                "世界自由金融申请美国国家银行牌照",
                published_at_ms=NOW_MS - 9_000,
            ),
            _row(
                "world-liberty-traditional",
                "世界自由金融申請美國國家銀行牌照",
                published_at_ms=NOW_MS - 8_000,
            ),
            _row(
                "nvidia-short-scale",
                "Nvidia targets $21bn SpaceX investment",
                published_at_ms=NOW_MS - 7_000,
            ),
            _row(
                "nvidia-upper-scale",
                "NVIDIA targets $21B SpaceX investment",
                published_at_ms=NOW_MS - 6_000,
            ),
            _row(
                "nvidia-full-number",
                "Nvidia targets $21,000,000,000.00 SpaceX investment",
                published_at_ms=NOW_MS - 5_000,
            ),
            _row(
                "anthropic-forecast",
                "Anthropic forecasts $70bn revenue by 2028",
                published_at_ms=NOW_MS - 4_000,
            ),
            _row(
                "anthropic-paraphrase",
                "Anthropic says revenue could reach $70 billion in 2028",
                published_at_ms=NOW_MS - 3_000,
            ),
        )
    )

    partitions = sorted(
        sorted(member["item_id"] for member in projection.memberships if member["story_id"] == story["story_id"])
        for story in projection.stories
    )
    assert partitions == [
        ["anthropic-forecast", "anthropic-paraphrase"],
        ["nvidia-full-number", "nvidia-short-scale", "nvidia-upper-scale"],
        ["world-liberty-simplified", "world-liberty-traditional"],
    ]


def test_exact_atom_retains_grounded_evidence_from_every_physical_item() -> None:
    provider_identity = ({"symbol": "BTC", "market_type": "cex", "match": "BTC"},)
    projection = build_story_projection(
        _snapshot(
            _row(
                "btc-exact-a",
                "BTC OI Rise 3.4% OI Value $21B Whale/OI Ratio 1.2",
                published_at_ms=NOW_MS - 1_000,
                provider_identity=provider_identity,
            ),
            _row(
                "btc-exact-b",
                "BTC OI Rise 3.4% OI Value $21B Whale/OI Ratio 1.2",
                published_at_ms=NOW_MS,
                provider_identity=provider_identity,
            ),
        )
    )

    assert len(projection.stories) == 1
    assert projection.stories[0]["identity_evidence"]["grounded_provider_count"] == 2


def test_rss_scores_physical_items_before_category_expansion_and_final_deduplication() -> None:
    _category, source = public_rss_membership_sources()[0]
    rows = tuple(
        {
            **_row(
                f"rss-physical-{index}",
                "Major earthquake strikes the same coastal region",
                published_at_ms=NOW_MS - index,
            ),
            "source_id": source.source_id,
            "reporting_origin": source.name,
            "tier": source.tier,
            "source_kind": "rss",
            "source_position": index,
            "memberships": source.memberships,
        }
        for index in range(2)
    )

    projection = build_story_projection(_snapshot(*rows))

    assert projection.population_item_ids == ("rss-physical-0", "rss-physical-1")
    assert len(projection.item_updates) == 2
    assert len(projection.stories) == 1
    assert projection.stories[0]["item_count"] == 2
    assert projection.diagnostics["input_physical_item_count"] == 2
    assert projection.diagnostics["population_physical_item_count"] == 2
    assert projection.diagnostics["exact_atom_count"] == 1
    assert projection.diagnostics["exact_membership_count"] == 1


def _golden_cases() -> list[dict[str, Any]]:
    return list(json.loads(GOLDEN_PATH.read_text())["cases"])


def _golden_snapshot(case: dict[str, Any], *, item_order: tuple[int, ...] | None = None) -> NewsStoryFactSnapshot:
    items = list(case["items"])
    if item_order is not None:
        items = [items[index] for index in item_order]
    return _snapshot(
        *(
            {
                **_row(
                    str(item["id"]),
                    str(item["title"]),
                    published_at_ms=NOW_MS - 60_000 + int(item.get("offset_ms", 0)),
                    provider_identity=tuple(dict(value) for value in item.get("provider_identity", ())),
                ),
                "reporting_origin": str(item.get("origin", f"wire-{item['id']}")),
            }
            for item in items
        )
    )


def _partitions(projection: Any) -> list[list[str]]:
    return sorted(
        sorted(member["item_id"] for member in projection.memberships if member["story_id"] == story["story_id"])
        for story in projection.stories
    )


@pytest.mark.parametrize("case", _golden_cases(), ids=lambda case: str(case["id"]))
def test_story_v2_versioned_golden_corpus(case: dict[str, Any]) -> None:
    projection = build_story_projection(_golden_snapshot(case))

    assert _partitions(projection) == sorted(sorted(partition) for partition in case["expected_partitions"])
    evidence = [story["identity_evidence"] for story in projection.stories]
    expected_reason = str(case["expected_reason"])
    if expected_reason != "per_item_sentinel":
        observed_reasons = {
            reason
            for story_evidence in evidence
            for field in ("membership_reasons", "rejection_reasons")
            for reason in story_evidence[field]
        }
        assert expected_reason in observed_reasons
    if "expected_source_count" in case:
        assert projection.stories[0]["source_count"] == case["expected_source_count"]
    if "expected_grounded_provider_count" in case:
        assert sum(value["grounded_provider_count"] for value in evidence) == case["expected_grounded_provider_count"]

    reversed_projection = build_story_projection(
        _golden_snapshot(case, item_order=tuple(reversed(range(len(case["items"])))))
    )
    assert reversed_projection.as_payload() == projection.as_payload()


@pytest.mark.parametrize(
    "case_id",
    ("fixed_anchor_blocks_transitive_bridge", "multi_anchor_tie_is_singleton", "untrackable_per_item_identity"),
)
def test_story_v2_is_invariant_to_every_permutation_for_three_item_adversaries(case_id: str) -> None:
    case = next(value for value in _golden_cases() if value["id"] == case_id)
    expected = build_story_projection(_golden_snapshot(case)).as_payload()
    for order in itertools.permutations(range(len(case["items"]))):
        assert build_story_projection(_golden_snapshot(case, item_order=order)).as_payload() == expected


def test_story_v2_golden_pairwise_quality_gate() -> None:
    true_positive = false_positive = false_negative = 0
    for case in _golden_cases():
        projection = build_story_projection(_golden_snapshot(case))
        actual_pairs = {
            tuple(sorted(pair))
            for partition in _partitions(projection)
            for pair in itertools.combinations(partition, 2)
        }
        expected_pairs = {
            tuple(sorted(pair))
            for partition in case["expected_partitions"]
            for pair in itertools.combinations(partition, 2)
        }
        true_positive += len(actual_pairs & expected_pairs)
        false_positive += len(actual_pairs - expected_pairs)
        false_negative += len(expected_pairs - actual_pairs)

    precision = true_positive / (true_positive + false_positive)
    recall = true_positive / (true_positive + false_negative)
    assert precision >= 0.98
    assert recall >= 0.90


def test_story_v2_candidate_pair_cap_is_exact_and_has_no_sampling_fallback() -> None:
    rows = tuple(
        _row(
            f"pair-{index}",
            f"Acme announces recurring market report sequence {index}",
            published_at_ms=NOW_MS - index,
        )
        for index in range(708)
    )
    with pytest.raises(NewsProjectionInputExceeded, match="news_story_candidate_pair_cap"):
        build_story_projection(_snapshot(*rows))


def test_story_v2_adversarial_boilerplate_stays_within_candidate_pair_bound() -> None:
    rows = tuple(
        _row(
            f"bounded-pair-{index}",
            f"Acme announces recurring market report sequence {index}",
            published_at_ms=NOW_MS - index,
        )
        for index in range(707)
    )
    assert len(build_story_projection(_snapshot(*rows)).stories) == 707


def test_story_v2_identity_evidence_bound_fails_explicitly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tracefold.news.story_projection.MAX_IDENTITY_EVIDENCE_BYTES", 1)
    with pytest.raises(NewsProjectionInputExceeded, match="news_story_identity_evidence_byte_cap"):
        build_story_projection(_snapshot(_row("bounded", "Central bank holds rates steady", published_at_ms=NOW_MS)))


def test_story_v2_result_carries_one_complete_versioned_desired_state() -> None:
    projection = build_story_projection(
        _snapshot(_row("complete", "Central bank holds rates steady", published_at_ms=NOW_MS))
    )
    payload = projection.as_payload()

    assert projection.projection_version == "news_story_projection_v2"
    assert len(projection.projection_fingerprint) == 64
    assert set(projection.versions) == {
        "comparison",
        "feature",
        "grounded_provider",
        "event_policy",
        "jaccard",
        "clustering",
        "identity",
        "classifier",
        "importance",
        "selector",
    }
    assert payload["projection_fingerprint"] == projection.projection_fingerprint
    assert payload["stories"] and payload["memberships"] and payload["item_updates"]
    assert payload["selection_snapshot"]["projection_revision"] == projection.material_snapshot_fingerprint
    assert projection.diagnostics == {
        "accepted_decision_count": 0,
        "ambiguity_split_count": 0,
        "candidate_pair_count": 0,
        "candidate_pair_peak": 0,
        "conflict_veto_count": 0,
        "event_family_counts": {"general": 1},
        "exact_atom_count": 1,
        "exact_membership_count": 0,
        "grounded_provider_count": 0,
        "input_encoded_bytes": projection.diagnostics["input_encoded_bytes"],
        "input_physical_item_count": 1,
        "population_physical_item_count": 1,
        "preliminary_rss_candidate_pair_count": 0,
        "rejected_decision_count": 0,
        "story_count": 1,
    }
    assert projection.diagnostics["input_encoded_bytes"] > 0
    assert payload["diagnostics"] == projection.diagnostics
