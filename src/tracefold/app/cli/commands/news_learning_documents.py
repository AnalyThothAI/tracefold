from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any


def _read_json_or_yaml(path: str) -> dict[str, Any]:
    """JSON first, YAML second.

    A frozen corpus is one line of JSON and can be megabytes; PyYAML is orders of magnitude slower on it, and
    YAML 1.1 does not resolve exponent-form floats without a decimal point — `1e-05` comes back as the *string*
    `"1e-05"`, which then fails the corpus hash check for no visible reason. A hand-written candidate file is
    still allowed to be YAML.
    """

    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    try:
        document = json.loads(text)
    except ValueError:
        import yaml

        document = yaml.safe_load(text)
    if not isinstance(document, dict):
        raise ValueError(f"news_document_not_a_mapping:{path}")
    return document


def _canonical_model_document(document: str, model_type: Any, *, code: str) -> Any:
    from tracefold.news.artifact_identity import canonical_json

    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        for key, value in pairs:
            if key in payload:
                raise ValueError(f"duplicate_key:{key}")
            payload[key] = value
        return payload

    try:
        payload = json.loads(
            document,
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
        )
        parsed = model_type.model_validate(payload)
    except (TypeError, ValueError) as exc:
        raise ValueError(code) from exc
    if document != canonical_json(parsed.model_dump(mode="json")):
        raise ValueError(code)
    return parsed


def _write_json(path: str, payload: Mapping[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
