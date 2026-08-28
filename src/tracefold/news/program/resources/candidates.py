"""Program candidates compiled into this image.

An evaluated ``CandidateManifest`` must be reviewed into this tuple before a
canary can be armed. Production resolves only registered, image-carried
``ProgramStrategyArtifactV1`` identities; database payloads never become executable
instructions or Python control flow.
"""

from __future__ import annotations

from typing import Any

from tracefold.news.learning.contracts import CandidateManifest

# Maintainers add sealed manifests here through normal code review. Keeping
# the initial registry empty is intentional: the current program_v8 epoch starts with
# the code-owned stable artifact and no optimizer candidate.
COMPILED_CANDIDATE_DOCUMENTS: tuple[dict[str, Any], ...] = ()


def compiled_canary_candidates() -> dict[str, CandidateManifest]:
    """Return valid image manifests without letting one bad document mask its siblings."""

    candidates: dict[str, CandidateManifest] = {}
    for value in COMPILED_CANDIDATE_DOCUMENTS:
        try:
            candidate = CandidateManifest.model_validate(value)
        except (TypeError, ValueError):
            continue
        candidates[candidate.candidate_sha] = candidate
    return candidates


__all__ = ["COMPILED_CANDIDATE_DOCUMENTS", "compiled_canary_candidates"]
