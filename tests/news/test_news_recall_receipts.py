"""The daily similarity proxy distinguishes proven links, anchors and missing vectors."""

from scripts.news_recall_receipts import measure, proxy_pairs
from tracefold.news.claim_recall import CALIBRATION, Candidate, text_sha, vector_bytes
from tracefold.news.notifications.novelty import ClaimLink
from tracefold.news.updates.contracts import Citation, Claim, ClaimFields


def fixture():
    claims = [
        Claim(
            ref=ref,
            statement=f"Agency imposes tariffs: {ref}.",
            fields=ClaimFields(subject="Agency", action="imposes tariffs", mode="decision"),
            citations=(Citation(evidence_ref="e1", quote="Agency imposes tariffs."),),
            first_available_at_ms=1,
        )
        for ref in ("a", "b")
    ]
    vector = vector_bytes([1.0, *([0.0] * (CALIBRATION.embedder.dimensions - 1))], CALIBRATION.embedder)
    candidates = {
        f"{c.ref}:{text_sha(c)}": Candidate(f"{c.ref}:{text_sha(c)}", vector, CALIBRATION.embedder.key) for c in claims
    }
    receipts = [
        {"intent_id": c.ref, "settled_at_ms": stamp, "sent_claims": [c.model_dump(mode="json")], "plan": None}
        for stamp, c in enumerate(claims, 2)
    ]
    return claims, candidates, receipts


def test_similar_sent_facts_without_coverage_are_a_proxy_and_two_hop_links_or_anchors_remove_it():
    claims, candidates, receipts = fixture()
    result = proxy_pairs(receipts, candidates, ())
    assert result["pairs"] == 1 and result["vector_comparisons"] == 1 and result["unknown_vector_pairs"] == 0
    links = (
        ClaimLink(current_ref="b", previous_ref="middle", relation="equivalent", asserted_at_ms=2),
        ClaimLink(current_ref="middle", previous_ref="a", relation="equivalent", asserted_at_ms=1),
    )
    assert proxy_pairs(receipts, candidates, links)["pairs"] == 0
    receipts[1]["plan"] = {"claim_decisions": [{"claim_ref": claims[1].ref, "reader": {"earlier": {"intent_id": "a"}}}]}
    assert proxy_pairs(receipts, candidates, ())["pairs"] == 0


def test_missing_vectors_and_equal_settlement_times_cannot_manufacture_a_negative_proxy():
    claims, candidates, receipts = fixture()
    key = f"{claims[0].ref}:{text_sha(claims[0])}"
    candidates[key] = Candidate(key)
    result = proxy_pairs(receipts, candidates, ())
    assert result["pairs"] == 0 and result["vector_comparisons"] == 0 and result["unknown_vector_pairs"] == 1
    receipts[1]["settled_at_ms"] = receipts[0]["settled_at_ms"]
    assert proxy_pairs(receipts, candidates, ())["unknown_vector_pairs"] == 0


def test_historical_unknown_calls_do_not_become_zero_degradation_or_a_zero_output_rate():
    statistics = {
        "relation_pairs": 0,
        "useful_relation_outputs": 0,
        "prior_calls": 0,
        "prior_degraded": 0,
        "receipt_calls": 0,
        "receipt_degraded": 0,
    }
    result = measure((statistics, [], {}, ()), as_of_ms=10)
    assert result["statistics"]["useful_relation_rate"] is None
    assert result["statistics"]["prior_degraded_fraction"] is None
    assert result["statistics"]["receipt_degraded_fraction"] is None
