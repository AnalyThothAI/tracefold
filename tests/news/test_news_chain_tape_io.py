from __future__ import annotations

import asyncio
from typing import Any

import pytest

from tracefold.news.bus import DeferError, TransientError
from tracefold.news.chain_tape.tape_io import FAILED, TapePasses


class _Database:
    async def read(self, name: str, fn: Any, *, timeout_seconds: float) -> Any:
        return fn(None)

    async def tx(self, name: str, fn: Any, *, timeout_seconds: float) -> Any:
        return fn(None)


class _Stage(TapePasses):
    db = _Database()
    _read_timeout_seconds = 1.0
    _write_timeout_seconds = 1.0
    _failure_stage = "research"


@pytest.mark.parametrize("operation", ["_read", "_write"])
def test_unexpected_pass_failure_reaches_the_stage_supervisor(operation: str) -> None:
    def invalid_row(_: Any) -> None:
        raise ValueError("unexpected row shape")

    errors: list[str] = []
    with pytest.raises(ValueError, match="unexpected row shape"):
        asyncio.run(getattr(_Stage(), operation)("test_rows", invalid_row, errors))
    assert errors == []


@pytest.mark.parametrize("operation", ["_read", "_write"])
@pytest.mark.parametrize("failure", [TransientError, DeferError])
def test_expected_refusal_is_retryable_and_logs_only_its_type(operation: str, failure: type[Exception], caplog) -> None:
    def refused(_: Any) -> None:
        raise failure("private failure detail")

    errors: list[str] = []
    assert asyncio.run(getattr(_Stage(), operation)("test_rows", refused, errors)) is FAILED
    assert errors == [f"db:{failure.__name__}"]
    assert failure.__name__ in caplog.text
    assert "private failure detail" not in caplog.text
