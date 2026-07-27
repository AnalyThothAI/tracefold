from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.postgres_test_utils import postgres_settings_storage, prepare_postgres_database
from tests.runtime_settings import disabled_workers_settings
from tracefold.app.http.app import _mount_frontend, create_app
from tracefold.platform.config.settings import PerWorkerSettings, Settings


def _disable_workers(settings: Settings) -> None:
    for name in settings.workers.__class__.model_fields:
        worker_settings = getattr(settings.workers, name)
        if isinstance(worker_settings, PerWorkerSettings):
            worker_settings.enabled = False


def test_frontend_dist_is_served_without_interfering_with_api(tmp_path):
    prepare_postgres_database()
    settings = Settings(
        ws_token="secret",
        storage=postgres_settings_storage(),
        workers=disabled_workers_settings(),
    )
    _disable_workers(settings)
    settings.set_config_dir(tmp_path / "app-home")
    dist = tmp_path / "dist"
    assets = dist / "assets"
    assets.mkdir(parents=True)
    (dist / "index.html").write_text(
        '<!doctype html><html><head><script type="module" src="/assets/app.js"></script></head></html>',
        encoding="utf-8",
    )
    (dist / "favicon.svg").write_text("<svg></svg>", encoding="utf-8")
    (assets / "app.js").write_text("window.__cockpit = true;", encoding="utf-8")

    app = create_app(settings=settings, start_collector=False, frontend_dist=dist)

    with TestClient(app) as client:
        home = client.get("/")
        app_route = client.get("/app")
        token_route = client.get("/token/CexToken/cex_token%3AZEC")
        retired_signal_lab_route = client.get("/signal-lab")
        news_route = client.get("/news")
        macro_route = client.get("/macro")
        macro_routes = [
            client.get(path)
            for path in (
                "/macro/research",
                "/macro/cross-asset",
                "/macro/rates-fed",
                "/macro/economy-inflation",
                "/macro/liquidity-funding",
                "/macro/credit",
                "/macro/volatility",
                "/macro/overview",
            )
        ]
        unknown_macro_route = client.get("/macro/not-a-page")
        retired_watchlist_route = client.get("/watchlist?handle=toly")
        asset = client.get("/assets/app.js")
        favicon = client.get("/favicon.svg")
        health = client.get("/healthz")
        missing_api = client.get("/api/not-a-route")

    assert home.status_code == 200
    assert "text/html" in home.headers["content-type"]
    assert home.headers["cache-control"] == "no-cache, max-age=0, must-revalidate"
    assert app_route.status_code == 200
    assert token_route.status_code == 200
    assert "text/html" in token_route.headers["content-type"]
    assert retired_signal_lab_route.status_code == 404
    assert news_route.status_code == 200
    assert "text/html" in news_route.headers["content-type"]
    assert macro_route.status_code == 200
    assert "text/html" in macro_route.headers["content-type"]
    assert all(response.status_code == 200 for response in macro_routes)
    assert all("text/html" in response.headers["content-type"] for response in macro_routes)
    assert unknown_macro_route.status_code == 404
    assert retired_watchlist_route.status_code == 404
    assert asset.status_code == 200
    assert "window.__cockpit" in asset.text
    assert asset.headers["cache-control"] == "no-cache, max-age=0, must-revalidate"
    assert favicon.status_code == 200
    assert favicon.headers["content-type"].startswith("image/svg+xml")
    assert favicon.headers["cache-control"] == "no-cache, max-age=0, must-revalidate"
    assert health.text == "ok\n"
    assert missing_api.status_code == 404


def test_frontend_dist_serves_browser_routes_for_spa(tmp_path):
    dist = tmp_path / "dist"
    assets = dist / "assets"
    assets.mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><html><body>cockpit</body></html>", encoding="utf-8")
    (assets / "app.js").write_text("window.__cockpit = true;", encoding="utf-8")

    app = FastAPI()
    _mount_frontend(app, frontend_dist=dist)

    with TestClient(app) as client:
        token_route = client.get("/token/CexToken/cex_token%3AZEC")
        retired_signal_lab_route = client.get("/signal-lab")
        news_route = client.get("/news")
        news_detail_route = client.get("/news/story/story_123")
        macro_route = client.get("/macro")
        macro_routes = [
            client.get(path)
            for path in (
                "/macro/research",
                "/macro/cross-asset",
                "/macro/rates-fed",
                "/macro/economy-inflation",
                "/macro/liquidity-funding",
                "/macro/credit",
                "/macro/volatility",
                "/macro/overview",
            )
        ]
        unknown_macro_route = client.get("/macro/not-a-page")
        retired_watchlist_route = client.get("/watchlist?handle=toly")
        missing_api = client.get("/api/not-a-route")

    assert token_route.status_code == 200
    assert "text/html" in token_route.headers["content-type"]
    assert token_route.headers["cache-control"] == "no-cache, max-age=0, must-revalidate"
    assert "cockpit" in token_route.text
    assert retired_signal_lab_route.status_code == 404
    assert news_route.status_code == 200
    assert "text/html" in news_route.headers["content-type"]
    assert news_detail_route.status_code == 200
    assert "text/html" in news_detail_route.headers["content-type"]
    assert macro_route.status_code == 200
    assert "text/html" in macro_route.headers["content-type"]
    assert all(response.status_code == 200 for response in macro_routes)
    assert all("text/html" in response.headers["content-type"] for response in macro_routes)
    assert unknown_macro_route.status_code == 404
    assert retired_watchlist_route.status_code == 404
    assert missing_api.status_code == 404
