"""The full-stack provider double consumes the same projected input as production.

Import DSPy-dependent helpers inside tests so broker-lane collection can load FastAPI first.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from tracefold.news.updates.contracts import ExtractionScope, FrozenInput
from tracefold.news.updates.identity import canonical_json
from tracefold.news.updates.projection import extraction_input


def test_scripted_provider_cites_only_visible_task_segments() -> None:
    from tests.golden._scripted_news_lm import _answer
    from tests.support.news_update_semantic import material

    evidence = material("1. Alpha opens a plant.\n2. Beta launches a product.\n3. Gamma closes an office.")
    source = FrozenInput(
        event_id="beta-event",
        revision=1,
        lineage_id="beta-line",
        evidence=(evidence,),
        extraction_scopes=(
            ExtractionScope(
                evidence_ref=evidence.ref,
                fact_id="beta",
                fact_text="Beta launches a product.",
                method="explicit_numbered",
            ),
        ),
    )
    request = SimpleNamespace(
        messages=(
            SimpleNamespace(
                parts=(SimpleNamespace(text=f"[[ ## evidence_json ## ]]\n{canonical_json(extraction_input(source))}"),)
            ),
        )
    )
    claims = _answer(request)["result"]["claims"]
    assert len(claims) == 1
    assert "Beta launches a product" in claims[0]["statement"]
    assert "Alpha opens" not in claims[0]["statement"]
    assert claims[0]["citations"] == [{"evidence_ref": evidence.ref, "quote": claims[0]["statement"]}]


def test_scripted_provider_runs_through_the_production_dspy_projection() -> None:
    from tests.golden._scripted_news_lm import scripted_generative_lm
    from tests.support.news_update_semantic import material
    from tracefold.news.adapters.extraction import DspyExtractor

    evidence = material("Binance will list ACMEUSDT perpetual futures on 2026-09-08")
    source = FrozenInput(event_id="golden-event", revision=1, lineage_id="golden-line", evidence=(evidence,))

    def factory():
        return scripted_generative_lm(SimpleNamespace(model_name="golden-model"), max_tokens=1000, timeout=30)

    extracted = asyncio.run(DspyExtractor(factory, model_identity="golden-model", topics={}).extract(source))
    assert len(extracted.claims) == 1
    assert extracted.claims[0].citations[0].evidence_ref == evidence.ref


def test_scripted_reader_provider_preserves_independent_evidence_and_requires_test_calibration() -> None:
    from tests.golden._scripted_news_lm import scripted_generative_lm, synthetic_reader_calibration
    from tests.support.news_update_semantic import update_one
    from tracefold.news.adapters.reader_judge import DspyReaderJudge
    from tracefold.news.notifications.novelty import ReaderNovelty
    from tracefold.news.notifications.policy import READER_CALIBRATIONS, reader_decision
    from tracefold.news.notifications.reader import ReaderInput
    from tracefold.news.updates.judgment import Budget

    _, _, adopted = update_one()
    reader = ReaderInput.of(adopted.claims[0], adopted, ())

    def factory():
        return scripted_generative_lm(SimpleNamespace(model_name="golden-model"), max_tokens=1000, timeout=30)

    judge = DspyReaderJudge(factory, generated_model_identity="golden-model")
    judgment = asyncio.run(judge.judge(reader, Budget.start(5)))
    assert judgment.status == "available" and judgment.backend == "generated"
    assert judgment.report_kind is not None and judgment.report_kind.value == "new_action"
    assert judgment.materiality is not None and judgment.materiality.probabilities == (0, 0, 1, 0)
    assert judgment.interrupt is not None and judgment.interrupt.probability == 0.05
    assert judgment.anchor is None

    with_message = ReaderInput.of(adopted.claims[0], adopted, ("An unrelated earlier announcement.",))
    compared = asyncio.run(judge.judge(with_message, Budget.start(5)))
    assert compared.status == "available"
    assert compared.anchor is not None and compared.anchor.probabilities == {"m1": 0.0, "none": 1.0}

    def decide(**options):
        return reader_decision(
            ReaderNovelty(novelty="unlinked"),
            judgment,
            first_available_at_ms=adopted.claims[0].first_available_at_ms,
            message_intents=(),
            **options,
        )

    # The provider factory is side-effect free: production defaults still refuse a model push.
    assert READER_CALIBRATIONS["generated"].certification_status == "uncalibrated"
    assert decide().outcome == "feed"
    assert decide(calibration=synthetic_reader_calibration()).outcome == "push"
