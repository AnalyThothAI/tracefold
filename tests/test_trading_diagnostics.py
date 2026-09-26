"""The bounded Trading diagnostic is read-only and does not invent Cache evidence."""

from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace
from typing import Any

from tracefold.app.cli.commands import trading


class _Rows:
    def fetchone(self) -> dict[str, str]:
        return {"version_num": "20260926_0402"}


class _Repos:
    def __init__(self) -> None:
        self.sql: list[str] = []
        self.conn = self
        self.trading = self

    def __enter__(self) -> _Repos:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def transaction(self) -> Any:
        return nullcontext()

    def execute(self, statement: str) -> _Rows:
        self.sql.append(statement)
        return _Rows()

    def execution_runtime_state(self, _slot: str) -> None:
        return None

    def execution_diagnostic_evidence(
        self, _slot: str
    ) -> tuple[tuple[dict[str, int], ...], tuple[dict[str, int], ...]]:
        return tuple({"n": n} for n in range(1001)), ({"seq": 1},)


def test_diagnose_has_real_sample_bounds_and_independent_source_clocks(monkeypatch: Any) -> None:
    repos = _Repos()
    monkeypatch.setattr(trading, "repositories", lambda *_args, **_kwargs: repos)
    settings = SimpleNamespace(
        trading=SimpleNamespace(
            execution=SimpleNamespace(
                enabled=True, account_slot="binance_usdm_primary", binance=SimpleNamespace(environment="DEMO")
            )
        ),
        ws_token="secret-do-not-print",
    )
    code, payload = trading._diagnose(SimpleNamespace(probe_url=None, status_url=None), settings=settings)
    data = payload["data"]
    assert code == 0 and len(data["database"]["open_plans"]) == 1000
    assert data["database"]["open_plans_truncated"] is True
    assert data["database"]["recent_risks"] == [{"seq": 1}]
    assert data["database"]["projection"]["entry_block_reason"] == "runtime_state_missing"
    assert data["started_at_ns"] <= data["database"]["started_at_ns"] <= data["completed_at_ns"]
    assert repos.sql == [
        "SET TRANSACTION READ ONLY",
        "SET LOCAL statement_timeout = '3s'",
        "SELECT version_num FROM alembic_version",
    ]
    assert "secret-do-not-print" not in str(payload)


def test_diagnostic_http_rejects_nonlocal_and_wrong_paths_without_network() -> None:
    assert trading._diagnostic_http("https://example.com/readyz", token=None)["error"] == "diagnostic_url_not_local"
    assert trading._diagnostic_http("http://serve:8765/other", token="secret")["error"] == (
        "diagnostic_url_path_invalid"
    )
