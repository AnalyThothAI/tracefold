"""The release verifier checks production selection and rejects incompatible vector bases."""

from __future__ import annotations

import numpy as np
import pytest

from scripts.verify_news_embedding import comparison_report, frozen_vectors


def test_frozen_vectors_preserve_both_rank_policies_and_real_reader_selection():
    frozen = frozen_vectors()
    report = comparison_report(frozen, frozen)
    assert report["vectors"]["count"] == report["vectors"]["identical_fp16_vectors"] == 55
    assert report["vectors"]["cosine_min"] >= 0.9999
    assert report["rank_compatibility"] == {
        consumer: {"queries": 55, "old_candidate_mismatches": 0, "mixed_candidate_mismatches": 0}
        for consumer in ("prior", "receipt")
    }
    assert len(report["issue_750"]["gold_receipts"]) == 4
    assert report["issue_750"]["negative_receipts"] == 0
    assert report["issue_755"]["unrelated_selected"] == 0


def test_verifier_rejects_orthogonal_basis_change_despite_preserving_pairwise_similarity():
    frozen = frozen_vectors()
    rotated = {
        text: np.roll(np.frombuffer(vector, dtype="<f2"), 1).astype("<f2").tobytes() for text, vector in frozen.items()
    }
    with pytest.raises(ValueError, match="cosine_failed"):
        comparison_report(frozen, rotated)
