"""An organically arriving member can reopen an old PR-suppressed Event."""

from types import SimpleNamespace

from tracefold.news.events.gate import GateInput, evaluate_gate
from tracefold.news.pipeline.admission import _member_result


def test_old_pr_suppression_reopens_without_a_stronger_provider_score() -> None:
    class News:
        upgraded = False

        def event_regate_context(self, event_id):
            return {
                "admission": "suppressed_pr_template",
                "leader_provider_metadata": {"score": 95},
                "leader_origin": "same-source",
                "storyline_key": "company",
            }

        def item_provider_score(self, item_id):
            return {"score": 20}

        def upgrade_event_admission(self, **kwargs):
            self.upgraded = True

    news = News()
    gate = evaluate_gate(
        GateInput(
            title="A company announces a new paid product",
            engine_type="news",
            provider_score=20,
            coins=(),
            ingest_mode="live",
        )
    )
    assert gate.admission == "candidate" and not gate.grounded_assets

    result = _member_result(
        SimpleNamespace(news=news),
        event_id="old-pr-event",
        item_id="new-item",
        inserted=True,
        match_kind="same_storyline",
        gate=gate,
        event_kind="news",
        dedupe_family="company",
        fingerprint="fingerprint",
        title="A company announces a new paid product",
        reporting_origin="same-source",
        grounded_assets_json="[]",
        watchlist_hits_json="[]",
        now_ms=1_000,
    )

    assert news.upgraded and result.admission == "candidate" and result.event_created
    assert result.evidence_focus_changed is False
