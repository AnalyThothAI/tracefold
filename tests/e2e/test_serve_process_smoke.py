"""Serve-process smoke: HTTP status surfaces over real PostgreSQL and uvicorn.

1. /readyz returns 200 with the PostgreSQL probe ok (schema at head).
2. /api/status reports the runtime block (no Workers process in this lane -> runtime not ok, db ok).
3. /api/news/status answers with the four-layer shape even before any Workers heartbeat.

This lane deliberately starts no Workers process. The RabbitMQ/Workers production path is
covered by the separate golden lane; retired route absence belongs to the OpenAPI contract.
"""

from __future__ import annotations

import httpx


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_serve_process_readyz(e2e_uvicorn: str) -> None:
    r = httpx.get(f"{e2e_uvicorn}/readyz", timeout=5.0)
    assert r.status_code == 200, f"readyz body: {r.text}"
    payload = r.json()
    assert (payload.get("db") or {}).get("ok") is True, f"PG probe not ok in /readyz payload: {payload}"


def test_serve_process_status_and_news_read_surfaces(e2e_uvicorn: str, e2e_ws_token: str) -> None:
    status = httpx.get(f"{e2e_uvicorn}/api/status", headers=_headers(e2e_ws_token), timeout=5.0)
    assert status.status_code == 200, status.text
    data = status.json()["data"]
    assert set(data) == {"measured_at_ms", "runtime"}
    assert data["runtime"]["db"]["ok"] is True
    assert data["runtime"]["workers_runtime"]["state"] == "unavailable"

    news = httpx.get(f"{e2e_uvicorn}/api/news/status", headers=_headers(e2e_ws_token), timeout=5.0)
    assert news.status_code == 200, news.text
    news_data = news.json()["data"]
    assert {"state", "ingest", "broker", "pipeline", "delivery"} <= set(news_data)
    assert "control" not in news_data
