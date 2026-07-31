from __future__ import annotations

from contextlib import contextmanager
from datetime import date
from types import SimpleNamespace

from tests.test_macro_thesis import CUTOFF_MS, SESSION
from tracefold.app.cli.commands import macro as macro_cli


class _FakeMacroRepository:
    def target_states(self):
        return []

    def recent_receipts(self, *, limit: int):
        assert limit == 20
        return []

    def all_modules_current(self):
        return []

    def document_analysis_job_state(self):
        return []


class _FakeMacroThesisRepository:
    def __init__(self) -> None:
        self.requested_session = None

    def state(self, session_date):
        self.requested_session = session_date
        return {
            "session_date": session_date,
            "status": "published",
            "schema_version": "macro_thesis_v1",
            "thesis_json": {"schema_version": "macro_thesis_v1"},
            "publication_id": "historical-v1-publication",
            "evidence_pack_id": "pack-v1",
            "research_input_id": None,
            "last_error_code": None,
            "last_gate_category": None,
        }

    def latest_live_delta(self, publication_id: str):
        raise AssertionError(f"v1 must not load current Live Delta: {publication_id}")

    def latest_outcome_replay(self, publication_id: str):
        raise AssertionError(f"v1 must not load current Outcome Replay: {publication_id}")

    def evaluation_evidence_packs(self):
        return []


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


def test_macro_status_is_current_session_only_and_reports_eval_readiness(monkeypatch) -> None:
    thesis_repo = _FakeMacroThesisRepository()
    fake_repos = SimpleNamespace(
        macro=_FakeMacroRepository(),
        macro_thesis=thesis_repo,
    )

    @contextmanager
    def fake_repositories(_settings):
        yield fake_repos

    monkeypatch.setattr(macro_cli, "load_settings", lambda **_kwargs: object())
    monkeypatch.setattr(macro_cli, "repositories", fake_repositories)
    monkeypatch.setattr(macro_cli, "_now_ms", lambda: CUTOFF_MS + 1_000)

    exit_code, payload = macro_cli._handle_status()

    assert exit_code == 0
    assert thesis_repo.requested_session == SESSION
    assert payload["data"]["current_session_date"] == SESSION.isoformat()
    assert payload["data"]["thesis"]["state"] == "not_published"
    assert payload["data"]["thesis"]["publication_id"] is None
    assert payload["data"]["live_delta"] is None
    assert payload["data"]["outcome_replay"] is None
    assert payload["data"]["offline_evaluation"] == {
        "schema_version": "macro_thin_eval_readiness_v1",
        "corpus_id": "macro_thin_profile_eval_v1",
        "baseline_commit": "810b9acc6fc5ea762fff43f1ce7efb8626960a84",
        "state": "collecting",
        "target_real_sessions": 9,
        "available_real_sessions": 0,
        "remaining_to_target": 9,
        "blocks_deployment": False,
        "session_dates": [],
        "selected_case_ids": [],
        "reason_code": "macro_eval_collecting_real_sessions",
    }
