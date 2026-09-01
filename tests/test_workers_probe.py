from __future__ import annotations

import threading

import pytest
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from tracefold.app.workers.probe import _create_workers_probe_app
from tracefold.app.workers.root import GRACEFUL_DRAIN_TIMEOUT_SECONDS, _ProbeState
from tracefold.app.workers.runtime import workers_runtime_status

RUNTIME_ID = "00000000-0000-0000-0000-000000000099"


def test_production_graceful_deadline_default_is_thirty_seconds() -> None:
    assert GRACEFUL_DRAIN_TIMEOUT_SECONDS == 30.0


def test_workers_probe_has_only_private_operational_routes_and_health_never_calls_readiness() -> None:
    calls = 0

    def readiness() -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"ok": False, "reason": "starting"}

    app = _create_workers_probe_app(readiness=readiness, render_metrics=lambda: "")
    with TestClient(app) as client:
        health = client.get("/healthz")
        ready = client.get("/readyz")

    assert health.status_code == 200
    assert calls == 1
    assert ready.status_code == 503
    assert {route.path for route in app.routes} >= {"/healthz", "/readyz", "/metrics"}
    assert "/telegram/control" not in {route.path for route in app.routes}


def test_workers_probe_mounts_the_control_ingress_only_when_it_is_explicitly_wired() -> None:
    async def control(_request: object) -> JSONResponse:
        return JSONResponse({"ok": True, "stage": "intent_recorded"})

    app = _create_workers_probe_app(
        readiness=lambda: {"ok": True},
        render_metrics=lambda: "",
        telegram_control=control,  # type: ignore[arg-type]
    )
    with TestClient(app) as client:
        response = client.post("/telegram/control", content=b"{}")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "stage": "intent_recorded"}


@pytest.mark.slow
def test_workers_metrics_render_does_not_block_readiness() -> None:
    metrics_entered = threading.Event()
    release_metrics = threading.Event()
    readiness_completed = threading.Event()

    def render_metrics() -> str:
        metrics_entered.set()
        assert release_metrics.wait(timeout=2.0)
        return "metric 1\n"

    app = _create_workers_probe_app(
        readiness=lambda: {"ok": True},
        render_metrics=render_metrics,
    )
    with TestClient(app) as client:
        metrics_response: list[object] = []
        readiness_response: list[object] = []
        metrics_thread = threading.Thread(
            target=lambda: metrics_response.append(client.get("/metrics")),
        )
        readiness_thread = threading.Thread(
            target=lambda: (readiness_response.append(client.get("/readyz")), readiness_completed.set()),
        )
        try:
            metrics_thread.start()
            assert metrics_entered.wait(timeout=1.0)
            readiness_thread.start()
            assert readiness_completed.wait(timeout=1.0), "readiness was blocked behind metrics rendering"
            assert metrics_response == [], "metrics must remain blocked until the test releases it"
        finally:
            release_metrics.set()
            metrics_thread.join(timeout=2.0)
            readiness_thread.join(timeout=2.0)

    assert len(readiness_response) == 1
    assert readiness_response[0].status_code == 200
    assert len(metrics_response) == 1
    assert metrics_response[0].status_code == 200


def test_workers_probe_fails_closed_when_persisted_heartbeat_is_stale() -> None:
    state = _ProbeState(
        runtime_id=RUNTIME_ID,
        runtime_version="v2",
        started_at_ms=1_000,
        clock_ms=lambda: 16_001,
        lifecycle_state="running",
        heartbeat_at_ms=1_000,
        ready=True,
        unavailable_reason="",
        runtime_manifest_sha="a" * 64,
    )

    assert state.payload()["runtime_manifest_sha"] == "a" * 64
    assert state.payload()["ok"] is False
    assert state.payload()["unavailable_reason"] == "runtime_heartbeat_stale"


@pytest.mark.parametrize(
    ("row", "query_failed", "state", "reason"),
    [
        (None, False, "unavailable", "runtime_missing"),
        (None, True, "unavailable", "runtime_status_query_failed"),
        (
            {
                "runtime_id": RUNTIME_ID,
                "runtime_version": "v2",
                "lifecycle_state": "running",
                "started_at_ms": 1_000,
                "heartbeat_at_ms": 1_000,
                "fatal_code": None,
            },
            False,
            "stale",
            "runtime_heartbeat_stale",
        ),
    ],
)
def test_workers_runtime_status_truth_table(row, query_failed, state, reason) -> None:
    status = workers_runtime_status(row, now_ms=20_000, query_failed=query_failed)
    assert status["state"] == state
    assert status["unavailable_reason"] == reason
    assert status["heartbeat_stale_after_ms"] == 15_000
