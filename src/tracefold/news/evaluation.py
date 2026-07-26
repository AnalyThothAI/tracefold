from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from itertools import combinations
from typing import Any


def clustering_metrics(
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, float | int | dict[str, int]]:
    """Measure candidate retrieval and final identity independently."""

    pairs = list(combinations(rows, 2))
    true_same = {(left["fact_id"], right["fact_id"]) for left, right in pairs if _same_truth(left, right)}
    predicted_same = {
        (left["fact_id"], right["fact_id"])
        for left, right in pairs
        if str(left["predicted_story_id"]) == str(right["predicted_story_id"])
    }
    true_positive = len(true_same & predicted_same)
    false_positive = len(predicted_same - true_same)
    false_negative = len(true_same - predicted_same)
    candidate_denominator = sum(1 for row in rows if bool(row.get("has_prior_true_member")))
    candidate_hits = sum(
        1 for row in rows if bool(row.get("has_prior_true_member")) and bool(row.get("true_story_recalled"))
    )
    bcubed_precision, bcubed_recall = _bcubed(rows)
    predicted_sizes = Counter(str(row["predicted_story_id"]) for row in rows)
    row_by_fact_id = {str(row["fact_id"]): row for row in rows}
    hard_negative_false_merges = sum(
        1
        for left_id, right_id in predicted_same - true_same
        if bool(row_by_fact_id[str(left_id)].get("hard_negative"))
        or bool(row_by_fact_id[str(right_id)].get("hard_negative"))
    )
    return {
        "candidate_recall": _ratio(candidate_hits, candidate_denominator),
        "pairwise_precision": _ratio(true_positive, true_positive + false_positive),
        "pairwise_recall": _ratio(true_positive, true_positive + false_negative),
        "false_merge_count": false_positive,
        "false_split_count": false_negative,
        "bcubed_precision": bcubed_precision,
        "bcubed_recall": bcubed_recall,
        "cluster_purity": _cluster_purity(rows),
        "hard_negative_false_merge_count": hard_negative_false_merges,
        "singleton_cluster_count": sum(1 for count in predicted_sizes.values() if count == 1),
        "cluster_size_distribution": dict(sorted(Counter(str(size) for size in predicted_sizes.values()).items())),
    }


def _bcubed(rows: Sequence[Mapping[str, Any]]) -> tuple[float, float]:
    if not rows:
        return 1.0, 1.0
    precision = 0.0
    recall = 0.0
    for row in rows:
        predicted = [item for item in rows if str(item["predicted_story_id"]) == str(row["predicted_story_id"])]
        truth = [item for item in rows if str(item["expected_cluster"]) == str(row["expected_cluster"])]
        overlap = sum(1 for item in predicted if str(item["expected_cluster"]) == str(row["expected_cluster"]))
        precision += _ratio(overlap, len(predicted))
        recall += _ratio(overlap, len(truth))
    return precision / len(rows), recall / len(rows)


def _cluster_purity(rows: Sequence[Mapping[str, Any]]) -> float:
    if not rows:
        return 1.0
    clusters: defaultdict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        clusters[str(row["predicted_story_id"])][str(row["expected_cluster"])] += 1
    return sum(max(counts.values()) for counts in clusters.values()) / len(rows)


def _same_truth(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    return str(left["expected_cluster"]) == str(right["expected_cluster"])


def _ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 1.0


__all__ = ["clustering_metrics"]
