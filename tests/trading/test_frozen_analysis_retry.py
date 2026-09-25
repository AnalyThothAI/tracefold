"""A reclaimed claim reuses the first complete evidence and candidate menu."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace

import pytest

from tracefold.app.analysis_files import AnalysisFiles
from tracefold.app.trading_analysis import AnalysisRunner
from tracefold.trading.engine.brief import build_brief
from tracefold.trading.engine.strategy import build_event_price_candidates


def test_reclaimed_analysis_restores_the_original_candidate_menu(tmp_path) -> None:
    files = AnalysisFiles(tmp_path)
    source = {"kind": "oi", "oi_change_bps": -400, "measurement_definition": "exchange-open-interest-v1"}
    bars = (
        *({"event_at_ms": (index + 1) * 60_000, "close": "100", "high": "101", "low": "99"} for index in range(15)),
        {"event_at_ms": 960_000, "close": "102", "high": "103", "low": "98"},
    )
    candidates = build_event_price_candidates(
        asset_id="crypto:SOL",
        instrument_semantics_digest="a" * 64,
        source_fact=source,
        source_first_visible_at_ms=930_000,
        perp_rows=bars,
    )
    menu = [candidate.model_dump(mode="json") for candidate in candidates]
    evidence_ref = files.write(
        {"case_id": "case-1", "candidate_menu": menu, "entry_reference": {"price": "102", "closed_at_ms": 960_000}}
    )
    brief = build_brief(
        target_asset_id="crypto:SOL",
        instrument_semantics_digest="a" * 64,
        source_fact=source,
        source_history=(),
        evidence={},
        features={},
        candidates=candidates,
    )
    brief_ref = files.write({"brief_json": brief.text})
    runner = SimpleNamespace(files=files)
    case = {"case_id": "case-1", "target_asset_id": "crypto:SOL", "mapping_semantics_digest": "a" * 64}
    prior = {"evidence_ref": evidence_ref, "brief_ref": brief_ref}

    restored = asyncio.run(AnalysisRunner._restore_prepared(runner, case, prior))
    assert restored.candidates == candidates
    assert restored.reference_price == Decimal("102")
    assert restored.brief.text == brief.text
    assert restored.evidence_ref == evidence_ref

    with pytest.raises(ValueError, match="frozen_analysis_snapshot_mismatch"):
        asyncio.run(AnalysisRunner._restore_prepared(runner, {**case, "case_id": "other"}, prior))
