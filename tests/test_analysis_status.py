"""The desk distinguishes an idle process, a missing model and a running Agent."""

from tracefold.app.analysis_status import analysis_status_projection
from tracefold.platform.config.models import Settings


def test_analysis_status_tracks_model_and_heartbeat() -> None:
    settings = Settings()
    settings.trading.enabled = True
    row = {
        "heartbeat_at_ms": 1_000,
        "active_policy": "trade_assessment_v1",
        "model_name": None,
        "model_configured": False,
        "publish_signals": False,
        "config_digest": "a" * 64,
    }
    assert analysis_status_projection(settings, None, now_ms=1_000, last_case_at_ms=None)["state"] == "unavailable"
    assert (
        analysis_status_projection(settings, row, now_ms=1_000, last_case_at_ms=None)["state"] == "model_unconfigured"
    )
    row["model_configured"] = True
    row["model_name"] = "fixture"
    assert analysis_status_projection(settings, row, now_ms=15_000, last_case_at_ms=None)["state"] == "running"
    assert analysis_status_projection(settings, row, now_ms=16_001, last_case_at_ms=None)["state"] == "unavailable"
