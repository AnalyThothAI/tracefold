import pytest
from fastapi.testclient import TestClient

from tests.postgres_test_utils import postgres_settings_storage
from tracefold.app.http.app import create_app
from tracefold.platform.config.models import Settings

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("postgres_dsn")]


def test_frontend_dist_is_served_without_interfering_with_api(tmp_path):
    settings = Settings(
        ws_token="secret",
        storage=postgres_settings_storage(),
    )
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

    app = create_app(settings=settings, frontend_dist=dist)

    with TestClient(app) as client:
        home = client.get("/")
        app_route = client.get("/app")
        token_route = client.get("/token/CexToken/cex_token%3AZEC")  # GMGN lane retired (#50)
        retired_signal_lab_route = client.get("/signal-lab")
        news_route = client.get("/news")
        retired_macro_routes = [
            client.get(path) for path in ("/macro", "/macro/overview", "/macro/rates-fed", "/macro/not-a-page")
        ]
        retired_watchlist_route = client.get("/watchlist?handle=toly")
        asset = client.get("/assets/app.js")
        favicon = client.get("/favicon.svg")
        health = client.get("/healthz")
        missing_api = client.get("/api/not-a-route")

    assert home.status_code == 200
    assert "text/html" in home.headers["content-type"]
    assert home.headers["cache-control"] == "no-cache, max-age=0, must-revalidate"
    assert app_route.status_code == 200
    assert token_route.status_code == 404
    assert retired_signal_lab_route.status_code == 404
    assert news_route.status_code == 200
    assert "text/html" in news_route.headers["content-type"]
    assert all(response.status_code == 404 for response in retired_macro_routes)
    assert retired_watchlist_route.status_code == 404
    assert asset.status_code == 200
    assert "window.__cockpit" in asset.text
    assert asset.headers["cache-control"] == "no-cache, max-age=0, must-revalidate"
    assert favicon.status_code == 200
    assert favicon.headers["content-type"].startswith("image/svg+xml")
    assert favicon.headers["cache-control"] == "no-cache, max-age=0, must-revalidate"
    assert health.text == "ok\n"
    assert missing_api.status_code == 404
