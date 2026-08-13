from __future__ import annotations

from contextlib import contextmanager
from datetime import date
from types import SimpleNamespace

from tracefold.app.cli.commands import macro as macro_cli


class _FakeMacroRepository:
    def all_acquisition_target_states(self):
        return [
            {
                "target_key": "steady-current",
                "clock_kind": "intraday",
                "status": "current",
                "next_due_at_ms": 2_000,
                "last_error_code": None,
            },
            {
                "target_key": "steady-due",
                "clock_kind": "daily",
                "status": "delayed",
                "next_due_at_ms": 900,
                "last_error_code": "upstream_403",
            },
            {
                "target_key": "steady-future",
                "clock_kind": "weekly",
                "status": "delayed",
                "next_due_at_ms": 3_000,
                "last_error_code": "upstream_403",
            },
            {
                "target_key": "maintenance",
                "clock_kind": "backfill",
                "status": "stale",
                "next_due_at_ms": 800,
                "last_error_code": "history_failed",
            },
            {
                "target_key": "steady-active-claim",
                "clock_kind": "daily",
                "status": "claimed",
                "next_due_at_ms": 800,
                "leased_until_ms": 1_100,
                "last_error_code": None,
            },
            {
                "target_key": "steady-expired-claim",
                "clock_kind": "daily",
                "status": "claimed",
                "next_due_at_ms": 700,
                "leased_until_ms": 950,
                "last_error_code": None,
            },
        ]

    def all_modules_current(self):
        return [
            {
                "module_id": "rates_fed",
                "current_health_state": "current",
                "history_depth_state": "complete",
                "fact_cutoff_ms": 123,
                "updated_at_ms": 456,
            }
        ]

    def document_analysis_job_state(self):
        return {"total": 0, "open": 0, "failed": 0, "completed": 0}


def test_explicit_backfill_enqueues_and_drains_before_returning(monkeypatch) -> None:
    calls: list[tuple[date, date, int]] = []

    class FakeMacroRepository:
        def enqueue_backfill_target(
            self,
            _spec,
            *,
            start_date,
            end_date,
            now_ms,
            max_attempts,
        ):
            calls.append((start_date, end_date, max_attempts))
            return {
                "target_key": "fred.dgs10:2026-01-01..2026-01-02",
                "status": "dirty",
                "next_due_at_ms": now_ms,
            }

    class FakeRepositories:
        macro = FakeMacroRepository()
        macro_market = SimpleNamespace()

        @contextmanager
        def transaction(self):
            yield

    settings = object()

    @contextmanager
    def fake_repositories(_settings):
        yield FakeRepositories()

    monkeypatch.setattr(macro_cli, "load_settings", lambda **_kwargs: settings)
    monkeypatch.setattr(macro_cli, "repositories", fake_repositories)
    monkeypatch.setattr(macro_cli, "_now_ms", lambda: 123)
    monkeypatch.setattr(
        macro_cli,
        "_drain_backfills",
        lambda received, *, target_keys: {
            "attempts": 1,
            "current": 1,
            "failed": 0,
            "results": [{"status": "current"}],
            "same_settings": received is settings,
            "target_keys": target_keys,
        },
    )

    exit_code, payload = macro_cli._handle_backfill(
        SimpleNamespace(
            dataset="fred.dgs10",
            start="2026-01-01",
            end="2026-01-02",
        )
    )

    assert exit_code == 0
    assert calls == [(date(2026, 1, 1), date(2026, 1, 2), 5)]
    assert payload["data"]["execution"]["same_settings"] is True
    assert payload["data"]["execution"]["target_keys"] == ("fred.dgs10:2026-01-01..2026-01-02",)
    assert payload["data"]["execution"]["current"] == 1


def test_macro_status_reports_targets_modules_and_document_analysis(monkeypatch) -> None:
    fake_repos = SimpleNamespace(macro=_FakeMacroRepository())
    settings = SimpleNamespace(
        llm=SimpleNamespace(
            api_key=None,
            base_url=None,
            macro_document_analysis_enabled=False,
            macro_document_analysis_model="gpt-5.4-mini",
        )
    )

    @contextmanager
    def fake_repositories(_settings):
        yield fake_repos

    monkeypatch.setattr(macro_cli, "load_settings", lambda **_kwargs: settings)
    monkeypatch.setattr(macro_cli, "repositories", fake_repositories)
    monkeypatch.setattr(macro_cli, "_now_ms", lambda: 1_000)
    exit_code, payload = macro_cli._handle_status()

    assert exit_code == 0
    assert "dataset_target_count" not in payload["data"]
    assert "target_status_counts" not in payload["data"]
    assert payload["data"]["acquisition"] == {
        "steady": {
            "target_count": 5,
            "actionable_due_count": 2,
            "oldest_actionable_due_at_ms": 900,
            "scheduled_future_count": 2,
            "in_progress_count": 1,
            "expired_claim_count": 1,
            "oldest_expired_claim_at_ms": 950,
            "status_counts": {"current": 1, "delayed": 2, "claimed": 2},
            "error_code_counts": {"upstream_403": 2},
        },
        "maintenance": {
            "target_count": 1,
            "in_progress_count": 0,
            "expired_claim_count": 0,
            "oldest_expired_claim_at_ms": None,
            "status_counts": {"stale": 1},
            "error_code_counts": {"history_failed": 1},
        },
    }
    assert payload["data"]["modules"] == [
        {
            "module_id": "rates_fed",
            "current_health_state": "current",
            "history_depth_state": "complete",
            "fact_cutoff_ms": 123,
            "updated_at_ms": 456,
        }
    ]
    assert payload["data"]["document_analysis_jobs"]["open"] == 0
    assert payload["data"]["document_analysis_runtime"] == {
        "state": "disabled",
        "enabled": False,
        "configured": False,
        "worker_active": False,
        "model": "gpt-5.4-mini",
    }
