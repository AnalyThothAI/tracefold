from fastapi.testclient import TestClient

from tracefold.app.process import create_probe_app


def test_nautilus_probe_answers_a_blocked_runtime_with_the_payload_that_says_why() -> None:
    """200 with the diagnosis, not 503 with nothing (#598 D5-b).

    `make runtime-status` fetched this endpoint with `curl -fsS`, so on the one process that owns
    live Binance exposure the answer to "what is wrong" was an empty body and a curl exit code. The
    payload is the answer; `ok` still carries the same claim, inside it.
    """

    readiness = {"ok": False, "execution_safe": False, "entry_block_reason": "startup_reconciliation"}
    app = create_probe_app(
        title="Tracefold Nautilus Probe",
        readiness=lambda: readiness,
        readiness_status_gate=False,
    )
    client = TestClient(app)

    assert client.get("/healthz").text == "ok\n"
    blocked = client.get("/readyz")
    assert blocked.status_code == 200
    assert blocked.json() == readiness

    readiness.update(ok=True, execution_safe=True, entry_block_reason=None)
    available = client.get("/readyz")
    assert available.status_code == 200
    assert available.json() == readiness
    # The Nautilus process publishes no Prometheus route: it is the one runtime whose metrics the
    # Workers registry never sees (#589 P-F16).
    assert {route.path for route in app.routes} == {"/healthz", "/readyz"}


def test_a_gated_probe_still_refuses_when_something_waits_on_it() -> None:
    """Workers keeps the 503: a Compose healthcheck and `make up` both wait on that endpoint."""

    readiness = {"ok": False, "reason": "startup"}
    app = create_probe_app(title="Tracefold Workers Probe", readiness=lambda: readiness)
    client = TestClient(app)

    unavailable = client.get("/readyz")
    assert unavailable.status_code == 503
    assert unavailable.json() == readiness

    readiness.update(ok=True, reason="ready")
    assert client.get("/readyz").status_code == 200
