from fastapi.testclient import TestClient

from tracefold.app.nautilus.probe import create_nautilus_probe_app


def test_nautilus_probe_separates_process_liveness_from_execution_readiness() -> None:
    readiness = {"ok": False, "reason": "startup_reconciliation"}
    client = TestClient(create_nautilus_probe_app(lambda: readiness))

    assert client.get("/healthz").text == "ok\n"
    unavailable = client.get("/readyz")
    assert unavailable.status_code == 503
    assert unavailable.json() == readiness

    readiness.update(ok=True, reason="ready")
    available = client.get("/readyz")
    assert available.status_code == 200
    assert available.json() == readiness
