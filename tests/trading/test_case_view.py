from decimal import Decimal

from tracefold.trading.engine.case_view import BaseRates, build_case_view
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
