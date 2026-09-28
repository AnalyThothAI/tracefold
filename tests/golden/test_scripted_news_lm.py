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
    from tracefold.news.updates.dspy_backend import DspyExtractor

    evidence = material("Binance will list ACMEUSDT perpetual futures on 2026-09-08")
    source = FrozenInput(event_id="golden-event", revision=1, lineage_id="golden-line", evidence=(evidence,))

    def factory():
        return scripted_generative_lm(SimpleNamespace(model_name="golden-model"), max_tokens=1000, timeout=30)

    extracted = asyncio.run(DspyExtractor(factory, model_identity="golden-model", topics={}).extract(source))
    assert len(extracted.claims) == 1
    assert extracted.claims[0].citations[0].evidence_ref == evidence.ref
