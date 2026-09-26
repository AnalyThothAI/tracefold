"""Read-only identity verification for retained News program images.

An old image is evidence of what ran, not executable state for the current graph.
Verify its original hashed projection without loading it through current Signatures.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from ..artifact_identity import canonical_sha, reject_nonfinite_json

# Both formats use this raw projection. Adding a format with different hash
# semantics requires a separate reader; never adapt the payload before hashing.
_HISTORY_SCHEMAS = frozenset({"news_program_state_v1", "news_program_state_v2"})
_IMAGE_FIELDS = frozenset(
    {"schema_version", "evidence_input_version", "program_sha256", "dspy_version", "predictors", "state"}
)


def historical_program_document(document: str | bytes, *, expected_sha: str) -> dict[str, Any]:
    """Return the unmodified stored document after checking its original identity."""

    try:
        raw = json.loads(document)
        if not isinstance(raw, dict) or set(raw) != _IMAGE_FIELDS:
            raise ValueError("news_program_previous_image_schema_invalid")
        if raw["schema_version"] not in _HISTORY_SCHEMAS:
            raise ValueError("news_program_previous_image_version_unsupported")
        reject_nonfinite_json(raw)
        state = raw["state"]
        predictors = raw["predictors"]
        if (
            not isinstance(state, dict)
            or not isinstance(predictors, list)
            or not predictors
            or any(not isinstance(name, str) for name in predictors)
            or len(predictors) != len(set(predictors))
            or set(predictors) != set(state)
            or any(not isinstance(entry, Mapping) for entry in state.values())
        ):
            raise ValueError("news_program_previous_image_schema_invalid")
        material = {
            "schema_version": raw["schema_version"],
            "evidence_input_version": raw["evidence_input_version"],
            "dspy_version": raw["dspy_version"],
            "predictors": predictors,
            "state": {
                name: {key: value for key, value in entry.items() if key != "lm"} for name, entry in state.items()
            },
        }
        if raw["program_sha256"] != expected_sha or canonical_sha(material) != expected_sha:
            raise ValueError("news_program_previous_image_identity_invalid")
        return raw
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("news_program_previous_image_schema_invalid") from exc
