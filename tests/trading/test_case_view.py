import hashlib
from dataclasses import asdict
from decimal import Decimal

import pytest

from tracefold.trading.engine.case_view import BaseRates, build_case_view, case_view_from_record
from tracefold.trading.engine.forecast import LegProbabilities
from tracefold.trading.engine.paper import LegGeometry


def test_case_view_exposes_aliases_without_identity_hashes_or_runtime_reads() -> None:
    digest = "a" * 64
    baseline = LegProbabilities(Decimal("0.4"), Decimal("0.3"), Decimal("0.3"))
    view = build_case_view(
        case_id=digest,
        asset_id="crypto:SOL",
        trigger_kind="catalyst",
        decided_at_ms=1_790_680_000_000,
        source_fact={
            "mode": "breaking",
            "phase": "confirmed",
            "polarity": "positive",
            "content_kind": "announcement",
            "change_kind": "new",
            "text": f"Source {digest} confirmed.",
        },
        features={"profile_version": "evidence_profile_v4", "perp_return_15m_bps": "23.5", "premium_bps": None},
        geometry=LegGeometry(100, 200),
        half_spread_bps=Decimal("1.5"),
        base_rates=(BaseRates("long", 12, baseline), BaseRates("short", 9, None)),
    )
    prompt = view.prompt_json()
    assert digest not in prompt
    assert "e1" in prompt and "23.5" in prompt
    assert "[digest]" in prompt
    assert "179068" not in prompt
    assert "11.5" in prompt
    assert view.features["perp_return_15m_bps"] == "23.5"
    assert view.base_rates[1].probabilities is None


def test_catalyst_projection_reports_missing_conditions_and_keeps_alias_identity() -> None:
    source = {
        "claims": [
            {"ref": "invalid", "fields": "invalid"},
            {
                "ref": "valid",
                "statement": "x" * 241,
                "fields": {
                    "subject": "x" * 161,
                    "conditions": ["x" * 121] * 5,
                    "quantities": list(range(5)),
                },
            },
        ]
    }
    view = build_case_view(
        case_id="b" * 64,
        asset_id="crypto:SOL",
        trigger_kind="catalyst",
        decided_at_ms=1000,
        source_fact=source,
        features={},
        geometry=LegGeometry(100, 200),
        half_spread_bps=Decimal(0),
        base_rates=(BaseRates("long", 0, None), BaseRates("short", 0, None)),
    )
    projected = view.model_input["source"]
    assert projected["claims_omitted"] == 1
    assert projected["projection_incomplete"]
    assert projected["claims"][0]["projection_coverage"] == {
        "statement_truncated": True,
        "fields_truncated": 1,
        "conditions_truncated": 4,
        "conditions_omitted": 1,
        "quantities_omitted": 1,
    }
    assert view.evidence_refs == {"e1": "valid"}


def _budget_view(context=(), source=None):
    return build_case_view(
        case_id="case",
        asset_id="crypto:PEPE",
        trigger_kind="catalyst",
        decided_at_ms=1000,
        source_fact=source
        or {
            "claims": [
                {
                    "ref": "claim-0",
                    "statement": "事件已确认",
                    "fields": {"subject": "PEPE", "action": "launch", "object": "network"},
                }
            ]
        },
        features={"profile_version": "evidence_profile_v4", "perp_return_15m_bps": "20", "data_status": {}},
        geometry=LegGeometry(100, 200),
        half_spread_bps=Decimal("1"),
        base_rates=(BaseRates("long", 0, None), BaseRates("short", 0, None)),
        recent_context=context,
    )


def _large_context(char):
    return tuple(
        {
            "trigger_id": f"context-{index}",
            "kind": "catalyst",
            "first_visible_at_ms": 900 - index,
            "payload": {
                "text": f"Observation {index}",
                "claims": [
                    {
                        "ref": f"claim-{index}",
                        "statement": char * 240,
                        "fields": {
                            "subject": char * 120,
                            "action": char * 120,
                            "object": char * 120,
                            "conditions": [char * 120] * 4,
                        },
                    }
                ],
            },
            "amendments": [],
        }
        for index in range(5)
    )


def test_context_within_budget_keeps_exact_existing_prompt_bytes():
    context = (
        {
            "trigger_id": "context-0",
            "kind": "catalyst",
            "first_visible_at_ms": 900,
            "payload": {"claims": [], "text": "之前的观察"},
            "amendments": [],
        },
    )
    prompt = _budget_view(context).prompt_json().encode()
    assert len(prompt) == 1599
    assert hashlib.sha256(prompt).hexdigest() == "309ccc4b6487356032dd68e5f20493675be87731a841d1e8161a3c973940bb3d"


def test_utf8_budget_removes_oldest_whole_context_and_records_omitted_aliases():
    ascii_view = _budget_view(_large_context("x"))
    assert len(ascii_view.model_input["recent_context"]) == 5
    assert "context_coverage" not in ascii_view.model_input
    context = _large_context("汉")
    view = _budget_view(context)
    coverage = view.model_input["context_coverage"]
    included = coverage["included"]
    assert 0 < included < 5
    assert coverage["provided"] == 5
    assert coverage["omitted"] == 5 - included
    assert coverage["reason"] == "input_budget"
    assert len(view.prompt_json().encode()) <= 16_384
    fitting_prefix = _budget_view(context[:included])
    assert view.model_input["recent_context"] == fitting_prefix.model_input["recent_context"]
    assert view.model_input["source"] == fitting_prefix.model_input["source"]
    assert view.geometry == fitting_prefix.geometry
    assert view.half_spread_bps == fitting_prefix.half_spread_bps
    assert view.base_rates == fitting_prefix.base_rates
    refs = {item["ref"] for item in view.model_input["facts"]}
    assert not refs.intersection(coverage["omitted_refs"])
    assert view.evidence_refs is not None
    assert [view.evidence_refs[ref] for ref in coverage["omitted_refs"]] == [
        fact["trigger_id"] for fact in context[included:]
    ]
    assert case_view_from_record(asdict(view)).prompt_json() == view.prompt_json()


def test_oversized_current_source_stays_fail_closed_without_silent_projection():
    source = {
        "claims": [
            {
                "ref": f"source-{index}",
                "statement": "汉" * 240,
                "fields": {key: "汉" * 160 for key in ("subject", "action", "object", "speaker")},
            }
            for index in range(12)
        ]
    }
    with pytest.raises(ValueError, match=r"^case_view_prompt_oversized$"):
        _budget_view(_large_context("x"), source)
