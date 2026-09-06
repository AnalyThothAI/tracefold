from __future__ import annotations

import json
import math

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tracefold.news.artifact_identity import canonical_json, canonical_sha
from tracefold.news.bus import BusDecodeError, BusMessage, decode_body
from tracefold.news.models import NEWS_BUS_SCHEMA_VERSION

pytestmark = pytest.mark.property

_TEXT = st.text(alphabet=st.characters(blacklist_categories=("Cs",)), max_size=128)
_IDENTIFIER_TEXT = _TEXT.filter(lambda value: bool(value) and len(value.encode("utf-8")) <= 128)
_SCALAR = st.none() | st.booleans() | st.integers(min_value=-(10**100), max_value=10**100) | _TEXT
_JSON = st.recursive(
    _SCALAR,
    lambda children: st.lists(children, max_size=6) | st.dictionaries(_TEXT, children, max_size=6),
    max_leaves=20,
)
_OBJECT = st.dictionaries(_TEXT, _JSON, max_size=12)


@given(_OBJECT)
def test_canonical_identity_is_order_independent_and_byte_deterministic(payload: dict[str, object]) -> None:
    reversed_payload = dict(reversed(tuple(payload.items())))

    assert canonical_json(payload) == canonical_json(reversed_payload)
    assert canonical_sha(payload) == canonical_sha(reversed_payload)
    assert canonical_json(payload).encode("utf-8") == canonical_json(payload).encode("utf-8")


def test_canonical_identity_preserves_unicode_codepoints_and_extreme_integers() -> None:
    payload = {"composed": "é", "combining": "e\u0301", "extreme": 10**200, "empty": None}

    assert canonical_json(payload) == (
        '{"combining":"é","composed":"é","empty":null,' + '"extreme":1' + "0" * 200 + "}"
    )
    assert canonical_sha(payload) == canonical_sha(payload)


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_canonical_identity_rejects_nonfinite_numbers(value: float) -> None:
    """Both halves of the identity refuse, and neither emits a token no JSON parser accepts.

    The message is CPython's wording, not this repository's contract: matching it makes an
    interpreter upgrade a red test, and it would still pass if `canonical_json` began emitting the
    bare `NaN` literal under some other message. What must hold is that no identity exists at all.
    """

    with pytest.raises(ValueError):
        canonical_json({"nested": [value]})
    with pytest.raises(ValueError):
        canonical_sha({"nested": [value]})


@given(
    kind=st.sampled_from(("raw", "event", "verdict")),
    message_id=_IDENTIFIER_TEXT,
    trace_id=_IDENTIFIER_TEXT,
    occurred_at_ms=st.integers(min_value=1, max_value=10**30),
    payload=_OBJECT,
    priority=st.integers(min_value=0, max_value=9),
    delivery_count=st.integers(min_value=0, max_value=100),
)
def test_bus_encode_decode_preserves_the_frozen_envelope(
    kind: str,
    message_id: str,
    trace_id: str,
    occurred_at_ms: int,
    payload: dict[str, object],
    priority: int,
    delivery_count: int,
) -> None:
    message = BusMessage(
        kind=kind,  # type: ignore[arg-type]
        message_id=message_id,
        routing_key="event.general.normal",
        payload=payload,
        trace_id=trace_id,
        occurred_at_ms=occurred_at_ms,
        priority=priority,
    )

    decoded = decode_body(
        message.body(),
        routing_key=message.routing_key,
        priority=priority,
        headers={"x-delivery-count": delivery_count},
    )

    assert decoded == BusMessage(
        kind=message.kind,
        message_id=message_id,
        routing_key=message.routing_key,
        payload=payload,
        trace_id=trace_id,
        occurred_at_ms=occurred_at_ms,
        priority=priority,
        # The broker counts failed deliveries from zero, so attempt 1 carries no header at all (#400).
        attempt=delivery_count + 1,
        headers={"x-delivery-count": delivery_count},
    )


def _envelope(**overrides: object) -> bytes:
    payload: dict[str, object] = {
        "schema_version": NEWS_BUS_SCHEMA_VERSION,
        "kind": "event",
        "message_id": "m",
        "trace_id": "t",
        "occurred_at_ms": 1,
        "payload": {},
    }
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def test_bus_boundary_rejects_unknown_fields_and_wrong_versions() -> None:
    with pytest.raises(BusDecodeError, match="news_bus_fields_invalid"):
        decode_body(_envelope(unknown=True), routing_key="event.x.normal", priority=0, headers=None)
    with pytest.raises(BusDecodeError, match="news_bus_schema_invalid"):
        decode_body(_envelope(schema_version="future"), routing_key="event.x.normal", priority=0, headers=None)


@pytest.mark.parametrize("timestamp", [None, True, 0, -1, 1.5, "1", math.nan, math.inf])
def test_bus_boundary_rejects_illegal_timestamps(timestamp: object) -> None:
    with pytest.raises(BusDecodeError, match="news_bus_timestamp_invalid"):
        decode_body(_envelope(occurred_at_ms=timestamp), routing_key="event.x.normal", priority=0, headers=None)


def test_bus_boundary_enforces_bounded_identifiers_and_nonfinite_payloads() -> None:
    decode_body(
        _envelope(message_id="m" * 128, trace_id="t" * 128),
        routing_key="event.x.normal",
        priority=0,
        headers=None,
    )
    for field in ("message_id", "trace_id"):
        with pytest.raises(BusDecodeError, match=f"news_bus_{field}_invalid"):
            decode_body(_envelope(**{field: "x" * 129}), routing_key="event.x.normal", priority=0, headers=None)
    for value in (math.nan, math.inf, -math.inf):
        with pytest.raises(ValueError):
            BusMessage(
                kind="event",
                message_id="m",
                routing_key="event.x.normal",
                payload={"not_finite": value},
                trace_id="t",
                occurred_at_ms=1,
            ).body()
