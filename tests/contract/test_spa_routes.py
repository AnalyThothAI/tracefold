from fastapi import FastAPI
from fastapi.testclient import TestClient

from tracefold.app.http.app import _mount_frontend


def test_frontend_dist_serves_browser_routes_for_spa(tmp_path) -> None:
    dist = tmp_path / "dist"
    assets = dist / "assets"
    assets.mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><html><body>cockpit</body></html>", encoding="utf-8")
    (assets / "app.js").write_text("window.__cockpit = true;", encoding="utf-8")

    app = FastAPI()
    _mount_frontend(app, frontend_dist=dist)

    with TestClient(app) as client:
        retired = [
            client.get("/token/CexToken/cex_token%3AZEC"),
            client.get("/signal-lab"),
            client.get("/macro"),
            client.get("/watchlist?handle=toly"),
        ]
        news = client.get("/news")
        detail = client.get("/news/story/story_123")
        trading = client.get("/trading")
        missing_api = client.get("/api/not-a-route")

    assert all(response.status_code == 404 for response in retired)
    assert news.status_code == detail.status_code == 200
    assert "text/html" in news.headers["content-type"]
    assert "text/html" in detail.headers["content-type"]
    assert trading.status_code == 200
    assert "text/html" in trading.headers["content-type"]
    assert missing_api.status_code == 404
