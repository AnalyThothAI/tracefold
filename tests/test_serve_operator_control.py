from __future__ import annotations

from typing import Any

import pytest

from tracefold.app.serve_runtime import bootstrap_serve
from tracefold.platform.config.models import Settings
from tracefold.trading import OperatorCommandError, parse_operator_command, prepare_parsed_operator_intent

_SEALED_NS = 1_900_000_000_000_000_000


def test_serve_requires_the_one_bearer_token_it_authenticates_every_route_with(tmp_path) -> None:
    settings = Settings(ws_token="")
    settings.set_config_dir(tmp_path)
    with pytest.raises(ValueError, match="ws_token is required"):
        bootstrap_serve(settings)


def _prepare(*, requested_at_ns: int, now_ns: int, text: str = "/pause operator console") -> Any:
    return prepare_parsed_operator_intent(
        parse_operator_command(text),
        source="cli",
        source_command_id="33333333-3333-4333-8333-333333333333",
        account_slot="binance_usdm_primary",
        operator_identity="uid:1000",
        authentication_identity="os:uid:1000",
        requested_at_ns=requested_at_ns,
        now_ns=now_ns,
    )


def test_operator_preparer_enforces_sealed_clock_and_expiry() -> None:
    skew_ns = 30_000_000_000
    assert _prepare(requested_at_ns=_SEALED_NS, now_ns=_SEALED_NS - skew_ns).value.action == "pause_entries"
    with pytest.raises(OperatorCommandError) as clock:
        _prepare(requested_at_ns=_SEALED_NS, now_ns=_SEALED_NS - skew_ns - 1)
    assert clock.value.code == "operator_command_clock_invalid"

    # A control TTL is 300 s: still an intent one nanosecond before it lapses, and not one after.
    ttl_ns = 300 * 1_000_000_000
    assert _prepare(requested_at_ns=_SEALED_NS, now_ns=_SEALED_NS + ttl_ns - 1).value.action == "pause_entries"
    with pytest.raises(OperatorCommandError) as expired:
        _prepare(requested_at_ns=_SEALED_NS, now_ns=_SEALED_NS + ttl_ns)
    assert expired.value.code == "operator_command_expired"
