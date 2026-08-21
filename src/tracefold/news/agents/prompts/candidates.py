"""Prompt candidates compiled into this image.

An evaluated CandidateManifest must be reviewed into this tuple before a
canary can be armed.  Production never reads Prompt text from PostgreSQL.
"""

from __future__ import annotations

from typing import Any

from tracefold.news.candidate_evaluator import CandidateManifest

# Maintainers add sealed manifests here through normal code review.  Keeping
# the initial registry empty is intentional: research notes are not a release.
COMPILED_CANDIDATE_DOCUMENTS: tuple[dict[str, Any], ...] = ()


def compiled_canary_candidates() -> dict[str, CandidateManifest]:
    candidates = [CandidateManifest.model_validate(value) for value in COMPILED_CANDIDATE_DOCUMENTS]
    return {candidate.candidate_sha: candidate for candidate in candidates}


__all__ = ["COMPILED_CANDIDATE_DOCUMENTS", "compiled_canary_candidates"]
