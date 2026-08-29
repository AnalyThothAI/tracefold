from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from typing import Any

from fastapi import Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel


def _json(payload: dict[str, Any], *, status_code: int = 200) -> JSONResponse:
    return JSONResponse(_finite_json(jsonable_encoder(payload)), status_code=status_code)


def _validated_json(
    schema: type[BaseModel],
    payload: Mapping[str, Any],
    *,
    status_code: int = 200,
) -> JSONResponse:
    """Validate an explicit API envelope before bypassing FastAPI serialization."""
    validated = schema.model_validate(payload)
    return _json(
        validated.model_dump(mode="json", by_alias=True, exclude_unset=True),
        status_code=status_code,
    )


def _validated_etag_json(
    schema: type[BaseModel],
    payload: Mapping[str, Any],
    *,
    data: Mapping[str, Any],
    etag_data: Mapping[str, Any] | None = None,
    request: Request,
    weak: bool = False,
) -> JSONResponse | Response:
    """Serve validated JSON with a representation-safe revalidation tag."""
    validated_payload = schema.model_validate(payload).model_dump(
        mode="json",
        by_alias=True,
        exclude_unset=True,
    )
    validated_data = _finite_json(validated_payload.get("data"))
    if validated_data != _finite_json(jsonable_encoder(data)):
        raise ValueError("etag_representation_data_mismatch")
    encoded = json.dumps(
        _finite_json(jsonable_encoder(etag_data)) if etag_data is not None else validated_data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    opaque_tag = f'"{hashlib.sha256(encoded).hexdigest()}"'
    etag = f"W/{opaque_tag}" if weak else opaque_tag
    headers = {"ETag": etag, "Cache-Control": "private, no-cache"}
    if weak:
        headers["Vary"] = "Accept-Encoding"
    else:
        # A strong validator must identify one byte-for-byte representation.
        # Prevent the outer compression middleware from changing those bytes.
        headers["Content-Encoding"] = "identity"
    if _if_none_match_matches(request.headers.get("if-none-match"), opaque_tag):
        return Response(status_code=304, headers=headers)
    response = _json(validated_payload)
    response.headers.update(headers)
    return response


def _etagged(data: dict[str, Any], request: Request, *, envelope: type[BaseModel]) -> JSONResponse | Response:
    return _validated_etag_json(envelope, {"ok": True, "data": data}, data=data, request=request)


def _if_none_match_matches(header: str | None, current_opaque_tag: str) -> bool:
    """Apply GET/HEAD weak comparison for a bounded server-generated ETag."""

    if header is None:
        return False
    for candidate in header.split(","):
        tag = candidate.strip()
        if tag == "*":
            return True
        if tag[:2].lower() == "w/":
            tag = tag[2:].strip()
        if tag == current_opaque_tag:
            return True
    return False


def _finite_json(value: Any) -> Any:
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {key: _finite_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_finite_json(item) for item in value]
    if isinstance(value, tuple):
        return [_finite_json(item) for item in value]
    return value
