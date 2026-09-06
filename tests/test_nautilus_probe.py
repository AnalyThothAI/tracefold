from fastapi.testclient import TestClient

from tracefold.app.process import create_probe_app


def test_nautilus_probe_separates_process_liveness_from_execution_readiness() -> None:
    readiness = {"ok": False, "reason": "startup_reconciliation"}
    app = create_probe_app(title="Tracefold Nautilus Probe", readiness=lambda: readiness)
    client = TestClient(app)

    assert client.get("/healthz").text == "ok\n"
    unavailable = client.get("/readyz")
    assert unavailable.status_code == 503
    assert unavailable.json() == readiness

    readiness.update(ok=True, reason="ready")
    available = client.get("/readyz")
    assert available.status_code == 200
    assert available.json() == readiness
    # The Nautilus process publishes no Prometheus route: it is the one runtime whose metrics the
    # Workers registry never sees (#589 P-F16).
    assert {route.path for route in app.routes} == {"/healthz", "/readyz"}
