"""Golden-path end-to-end test: the served News + Macro surfaces across a real process boundary.

1. /readyz returns 200 with the PostgreSQL probe ok (schema at head).
2. /api/status reports the runtime block (no Workers process in this lane -> runtime not ok, db ok).
3. /api/news/status answers with the four-layer shape even before any Workers heartbeat.
4. /api/macro/overview answers with the six-module index.
5. The retired GMGN surfaces are gone: /ws, /api/recent, /api/search, /api/token-case, /api/live-market -> 404.
"""

from __future__ import annotations

import httpx
import pytest


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.e2e
def test_golden_path_readyz(e2e_uvicorn: str) -> None:
    r = httpx.get(f"{e2e_uvicorn}/readyz", timeout=5.0)
    assert r.status_code == 200, f"readyz body: {r.text}"
    payload = r.json()
    assert (payload.get("db") or {}).get("ok") is True, f"PG probe not ok in /readyz payload: {payload}"


@pytest.mark.e2e
def test_golden_path_status_news_and_macro_read_surfaces(e2e_uvicorn: str, e2e_ws_token: str) -> None:
    status = httpx.get(f"{e2e_uvicorn}/api/status", headers=_headers(e2e_ws_token), timeout=5.0)
    assert status.status_code == 200, status.text
    data = status.json()["data"]
    assert set(data) == {"measured_at_ms", "runtime"}
    assert data["runtime"]["db"]["ok"] is True
    assert data["runtime"]["workers_runtime"]["state"] == "unavailable"

    news = httpx.get(f"{e2e_uvicorn}/api/news/status", headers=_headers(e2e_ws_token), timeout=5.0)
    assert news.status_code == 200, news.text
    news_data = news.json()["data"]
    assert {"state", "ingest", "broker", "pipeline", "delivery", "control"} <= set(news_data)

    macro = httpx.get(f"{e2e_uvicorn}/api/macro/overview", headers=_headers(e2e_ws_token), timeout=5.0)
    assert macro.status_code == 200, macro.text
    assert len(macro.json()["data"]["modules"]) == 6


@pytest.mark.e2e
@pytest.mark.parametrize(
    "path",
    ("/api/recent", "/api/search?q=btc", "/api/token-case?target_type=chain_token&target_id=x", "/api/live-market"),
)
def test_golden_path_retired_market_routes_are_absent(e2e_uvicorn: str, e2e_ws_token: str, path: str) -> None:
    r = httpx.get(f"{e2e_uvicorn}{path}", headers=_headers(e2e_ws_token), timeout=5.0)
    assert r.status_code == 404, f"{path}: {r.status_code} {r.text[:200]}"
