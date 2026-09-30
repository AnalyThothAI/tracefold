"""Current read-only Trading HTTP contract and retired route proof."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tracefold.app.http.app import create_app
from tracefold.platform.config.models import Settings

TOKEN = "trading-contract-token"
CASE_ID = "a" * 64


class _Trading:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def latest_case_created_at_ms(self) -> None:
        return None

    def analysis_runtime(self, _account_slot: str) -> None:
        return None

    def state(self, _account_slot: str) -> None:
        return None

    def control(self, _account_slot: str) -> None:
        return None

    def analysis_cases(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(("analysis_cases", kwargs))
        return [
            {
                "case_id": CASE_ID,
                "trigger_kind": "oi",
                "asset_id": "crypto:SOL",
                "native_symbol": "SOLUSDT",
                "created_at_ms": 1_900_000_000_000,
                "state": "complete",
                "failure_code": None,
                "decided_at_ms": 1_900_000_001_000,
                "geometry_version": "leg_geometry_v1",
                "view_sha256": "b" * 64,
                "raw_snapshot_ref": "c" * 64,
            }
        ]

    def analysis_case(self, case_id: str) -> dict[str, Any] | None:
        self.calls.append(("analysis_case", {"case_id": case_id}))
        if case_id != CASE_ID:
            return None
        return {
            **self.analysis_cases()[0],
            "view": {"model_input": {"trigger": "oi"}},
            "assessments": [],
            "policy_actions": [],
            "paper_legs": [],
        }

    def scoreboard(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("scoreboard", kwargs))
        return {
            "window": {"since_ms": kwargs["since_ms"], "until_ms": kwargs["until_ms"]},
            "funnel": {
                "triggers": 1,
                "selected": 1,
                "assessed": 0,
                "published": 0,
                "execution_accepted": 0,
                "filled": 0,
            },
            "programs": [],
            "comparisons": [],
        }


class _Runtime:
    def __init__(self, settings: Settings, trading: _Trading) -> None:
        self.settings = settings
        self.trading = trading

    @contextmanager
    def repositories(self):
        yield SimpleNamespace(trading=self.trading)


@pytest.fixture()
def client(tmp_path: Path) -> tuple[TestClient, _Trading]:
    settings = Settings(ws_token=TOKEN)
    settings.set_config_dir(tmp_path)
    trading = _Trading()
    app = create_app(settings=settings)
    app.state.service = _Runtime(settings, trading)
    return TestClient(app), trading


def test_status_is_uncached_and_names_disabled_processes(client: tuple[TestClient, _Trading]) -> None:
    http, _ = client
    response = http.get("/api/trading/status", params={"token": TOKEN}, headers={"If-None-Match": '"old"'})
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    data = response.json()["data"]
    assert data["decision"]["state"] == "disabled"
    assert data["decision"]["program_sha"] is None
    assert data["execution"]["entries_armed"] is False


def test_case_list_detail_and_scoreboard_have_one_current_contract(client: tuple[TestClient, _Trading]) -> None:
    http, trading = client
    listing = http.get("/api/trading/cases", params={"token": TOKEN})
    assert listing.status_code == 200
    assert listing.json()["data"]["cases"][0]["case_id"] == CASE_ID
    detail = http.get("/api/trading/cases", params={"token": TOKEN, "case_id": CASE_ID})
    assert detail.status_code == 200
    assert detail.json()["data"]["cases"][0]["view"]["model_input"]["trigger"] == "oi"
    assert http.get("/api/trading/cases", params={"token": TOKEN, "case_id": "bad"}).status_code == 400
    assert http.get("/api/trading/cases", params={"token": TOKEN, "state": "WATCH"}).status_code == 400
    assert http.get("/api/trading/cases", params={"token": TOKEN, "underlying": "SOL"}).status_code == 400
    board = http.get("/api/trading/scoreboard", params={"token": TOKEN, "program": "d" * 64})
    assert board.status_code == 200
    assert board.json()["data"]["funnel"]["triggers"] == 1
    assert next(value for name, value in trading.calls if name == "scoreboard")["program_sha"] == "d" * 64
    assert http.get("/api/trading/scoreboard", params={"token": TOKEN, "program": "bad"}).status_code == 400


def test_retired_paths_are_absent_and_current_paths_require_auth(client: tuple[TestClient, _Trading]) -> None:
    http, _ = client
    for path in ("/api/trading/gate", "/api/trading/signals", "/api/trading/intents"):
        assert http.get(path, params={"token": TOKEN}).status_code == 404
    for path in ("/api/trading/status", "/api/trading/cases", "/api/trading/scoreboard", "/api/trading/executions"):
        assert http.get(path).status_code == 401
