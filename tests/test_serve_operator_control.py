from __future__ import annotations

from contextlib import contextmanager
from typing import Any, cast
from uuid import UUID

import pytest

from tracefold.app.http.exceptions import ApiUnavailable
from tracefold.app.serve_database import ServeDatabase
from tracefold.app.serve_runtime import ServeRuntime, bootstrap_serve
from tracefold.platform.config.models import Settings
from tracefold.platform.observability import TelemetryRegistry
from tracefold.trading import parse_operator_command, prepare_parsed_operator_intent


def test_operator_command_uses_one_dedicated_short_write_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class Trading:
        def append_operator_intent(self, prepared: Any) -> tuple[int, bool]:
            events.append(f"append:{prepared.value.action}")
            return (41, True)

    class Repositories:
        trading = Trading()

        @contextmanager
        def transaction(self):
            events.append("transaction:begin")
            try:
                yield
            finally:
                events.append("transaction:end")

    @contextmanager
    def open_repositories(settings: Settings, *, application_name: str):
        assert settings is runtime.settings
        events.append(f"open:{application_name}")
        yield Repositories()
        events.append("close")

    monkeypatch.setattr("tracefold.app.serve_runtime.open_repositories", open_repositories)
    runtime = _runtime()
    prepared = prepare_parsed_operator_intent(
        parse_operator_command("/pause operator console"),
        source="http",
        source_command_id="11111111-1111-4111-8111-111111111111",
        account_slot="binance_usdm_primary",
        operator_identity="http:operator",
        authentication_identity="bearer:test",
        requested_at_ns=1_900_000_000_000_000_000,
    )

    receipt = runtime.persist_operator_intent(prepared)

    assert receipt.command_id == prepared.value.command_id
    assert receipt.seq == 41
    assert receipt.disposition == "awaiting_runtime"
    assert events == [
        "open:tracefold_serve_operator_control",
        "transaction:begin",
        "append:pause_entries",
        "transaction:end",
        "close",
    ]


def test_operator_command_fails_closed_when_the_single_writer_is_busy() -> None:
    runtime = _runtime()
    prepared = prepare_parsed_operator_intent(
        parse_operator_command("/pause operator console"),
        source="http",
        source_command_id="22222222-2222-4222-8222-222222222222",
        account_slot="binance_usdm_primary",
        operator_identity="http:operator",
        authentication_identity="bearer:test",
        requested_at_ns=1_900_000_000_000_000_000,
    )
    assert runtime.operator_command_gate.acquire(blocking=False)
    try:
        with pytest.raises(ApiUnavailable, match="operator_command_busy"):
            runtime.persist_operator_intent(prepared)
    finally:
        runtime.operator_command_gate.release()


def test_serve_requires_the_one_bearer_token_it_authenticates_every_route_with(tmp_path) -> None:
    settings = Settings(ws_token="")
    settings.set_config_dir(tmp_path)

    with pytest.raises(ValueError, match="ws_token is required"):
        bootstrap_serve(settings)


def _runtime() -> ServeRuntime:
    return ServeRuntime(
        settings=Settings(),
        db=cast(ServeDatabase, object()),
        telemetry=cast(TelemetryRegistry, object()),
        runtime_id=UUID("11111111-1111-4111-8111-111111111111"),
        runtime_revision="test-revision",
        image_digest="sha256:" + "a" * 64,
        started_at_ms=1_900_000_000_000,
    )
