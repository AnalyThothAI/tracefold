"""The one `dspy.Predict` call: what it may see, what it returns, and what every failure becomes.

The important assertions are negative ones. A model that moves money is safe because of what it cannot
reach and what happens when it misbehaves, not because of what the prompt asks for.
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from dspy.utils.dummies import DummyLM

from tracefold.trading.contracts import (
    FrozenMarketContext,
    FrozenStrategyContext,
    InstrumentRef,
    NewsTradeCandidate,
    OiMarketTrigger,
    OiRegime,
    OiTradeCandidate,
    RegimeAssessment,
    TradingCaseManifest,
)
from tracefold.trading.decision.program import (
    TradingDecisionProgram,
    assert_model_input_safe,
    build_inputs,
    program_sha256,
)
from tracefold.trading.strategy.root import strategies

NOW = 1_787_000_000_000

_ANSWER = {
    "decision": "long",
    "directness": "direct",
    "surprise": 3,
    "price_in": 0,
    "alignment": "aligned",
    "horizon": "hours",
    "reason_code": "none",
    "thesis_zh": "上币公告带来新增需求",
    "invalidation_zh": "公告被撤回",
}


_NEWS = NewsTradeCandidate(
    event_id="n1",
    verdict_created_at_ms=NOW,
    opened_at_ms=NOW,
    base_symbol="DOGE",
    evidence_version=3,
    evidence_sha256="abc",
    focus_fact_id="f1",
    comparison_fingerprint="fp",
    source_artifact_id="x:123",
    source_published_at_ms=NOW,
    final_decision="push",
    event_type="listing",
    risk_direction="bullish",
    scope="single_name",
    magnitude=2,
    novelty="new_fact",
    headline_zh="币安上线 DOGE 永续",
    why_zh="新增可交易场所带来增量需求",
    learning_epoch="bundle_00000000",
    program_version="news_semantic_program_v5",
    program_sha256="a" * 64,
    policy_version="news_triage_policy_v10",
    editorial_origin="model",
    editorial_sha256="b" * 64,
    scored_judgment_sha256="c" * 64,
    runtime_manifest_sha="d" * 64,
)


def _manifest(*, news: NewsTradeCandidate | None = None) -> TradingCaseManifest:
    oi = OiTradeCandidate(
        event_id="e1",
        observed_at_ms=NOW,
        verdict_created_at_ms=NOW,
        base_symbol="DOGE",
        venue="hyperliquid",
        oi_direction="rise",
        oi_change_bps=320,
        oi_value_usd=73_010_000,
        whale_long_profit_bps=9_900,
        whale_oi_ratio_bps=21_097,
        rank_in_window=1,
        final_decision="push",
        source_rule="opening_move_with_whale_concentration",
        metric_version="oi_signal_v1",
        learning_epoch="bundle_00000000",
        program_version="news_oi_signal_v1",
        program_sha256="a" * 64,
        policy_version="news_triage_policy_v10",
        editorial_origin="telemetry_deterministic",
        editorial_sha256="b" * 64,
        scored_judgment_sha256="c" * 64,
        runtime_manifest_sha="d" * 64,
    )
    regime = RegimeAssessment(
        regime=OiRegime.BUILDUP_UP,
        reason="quadrant",
        pre_move_bps=210,
        oi_direction="rise",
    )
    market = FrozenMarketContext(
        mark_price=Decimal("0.4123"),
        observed_at_ms=NOW,
        pre_move_bps=210,
        pre_move_lookback_ms=3_600_000,
    )
    instrument = InstrumentRef(
        exchange_id="binance",
        venue="binance.perp",
        provider_symbol="DOGEUSDT",
        base_symbol="DOGE",
        instrument_class="crypto",
        observed_at_ms=NOW,
    )
    strategy = strategies()["news_oi_alignment_v1" if news is not None else "oi_momentum_v1"]
    return TradingCaseManifest(
        primary_trigger=OiMarketTrigger(
            source_key=oi.source_key,
            observed_at_ms=NOW,
            persisted_at_ms=NOW,
            venue=oi.venue,
        ),
        contexts=FrozenStrategyContext(
            mode="paper",
            oi=oi,
            news=news,
            regime=regime,
            market=market,
        ),
        strategy_id=strategy.strategy_id,
        strategy_version=strategy.strategy_version,
        strategy_config=strategy.config_snapshot,
        strategy_config_digest=strategy.config_digest,
        underlying_key="crypto:DOGE",
        base_symbol="DOGE",
        cutoff_ms=NOW,
        instrument=instrument,
    )


class _CountingPredictor:
    """Stands in for the compiled Predictor so a physical call is countable."""

    def __init__(self, result: Any) -> None:
        self.calls = 0
        self._result = result

    async def acall(self, **_: Any) -> Any:
        self.calls += 1
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class _Prediction:
    def __init__(self, **fields: Any) -> None:
        for key, value in fields.items():
            setattr(self, key, value)


def _program(result: Any) -> tuple[TradingDecisionProgram, _CountingPredictor]:
    program = TradingDecisionProgram(lm=DummyLM([_ANSWER]), model_name="test/model")
    predictor = _CountingPredictor(result)
    program._predictor = predictor  # type: ignore[attr-defined] - injecting the compiled Predictor
    return program, predictor


# ---------------------------------------------------------------------------- what the model may see
@pytest.mark.parametrize("news", [None, _NEWS], ids=["oi_only", "with_news_evidence"])
def test_the_model_never_receives_an_account_credential_or_order_field(news: NewsTradeCandidate | None) -> None:
    """Both shapes, because the original test only ever built the empty one.

    It declared `kind="news_oi"` while passing `news=None` — a combination `_freeze` cannot produce —
    so `news_snapshot_json` was the literal `"{}"` and the only document carrying real evidence text
    was never scanned. Renaming any `NewsTradeCandidate` field into `_FORBIDDEN_INPUT_KEYS` would have
    left this green while every real case raised inside `decide()` and degraded to a silent no-trade.
    """

    inputs = build_inputs(_manifest(news=news))
    if news is not None:
        assert inputs["news_snapshot_json"] != "{}"
    blob = " ".join(inputs.values()).lower()
    for forbidden in ("account", "api_key", "balance", "quantity", "notional", "stop_price", "hedged", "token"):
        assert forbidden not in blob, forbidden


def test_the_oi_audit_stamp_never_reaches_the_prompt_that_program_sha256_addresses() -> None:
    """#211 added `verdict_created_at_ms` to `OiTradeCandidate`, and the prompt must not notice.

    `program_sha256` hashes the version, the model, the instruction and the signature — not the
    inputs — so a new field in the serialised candidate would change what the model reads while every
    case kept the same program identity, and any replay or A/B keyed on that identity would pool two
    populations. The News side's field of the same name is its own point-in-time cutoff and is real
    evidence, so this is an exclusion at the OI document, not a forbidden name.
    """

    inputs = build_inputs(_manifest(news=_NEWS))
    assert "verdict_created_at_ms" not in json.loads(inputs["oi_context_json"])
    assert "verdict_created_at_ms" in json.loads(inputs["news_snapshot_json"])


def test_a_document_carrying_a_forbidden_key_refuses_to_become_a_prompt() -> None:
    with pytest.raises(ValueError, match="trading_model_input_forbidden_key"):
        assert_model_input_safe({"market": {"balance": 1000}})
    with pytest.raises(ValueError, match="trading_model_input_forbidden_key"):
        assert_model_input_safe({"nested": [{"inner": {"api_key": "x"}}]})


def test_the_three_inputs_are_bounded_json_and_nothing_else() -> None:
    inputs = build_inputs(_manifest())
    assert set(inputs) == {"news_snapshot_json", "oi_context_json", "market_context_json"}
    assert inputs["news_snapshot_json"] == "{}"  # an OI-only case carries no News snapshot


def test_program_identity_is_content_addressed_and_changes_with_the_model() -> None:
    first = program_sha256(model_name="a", instruction="i")
    assert first == program_sha256(model_name="a", instruction="i")
    assert first != program_sha256(model_name="b", instruction="i")
    assert first != program_sha256(model_name="a", instruction="j")


# ---------------------------------------------------------------------------- one call, and failure is no-trade
def test_exactly_one_physical_call_per_invocation() -> None:
    program, predictor = _program(_Prediction(**_ANSWER))
    result = asyncio.run(program.decide(_manifest()))
    assert predictor.calls == 1
    assert result.decision.decision == "long"
    assert result.trace["calls"] == 1
    assert result.identity is not None
    assert result.trace["manifest_sha256"] == _manifest().digest()


def test_a_provider_failure_is_a_no_trade_and_never_raises() -> None:
    program, predictor = _program(RuntimeError("boom"))
    result = asyncio.run(program.decide(_manifest()))
    assert result.decision.decision == "no_trade"
    assert result.trace["error"].startswith("provider:")
    # Still exactly one attempt: there is no fast retry and no fallback endpoint.
    assert predictor.calls == 1


def test_a_deadline_is_a_no_trade() -> None:
    program = TradingDecisionProgram(lm=DummyLM([_ANSWER]), model_name="test/model", deadline_seconds=0.01)

    class _Slow:
        async def acall(self, **_: Any) -> Any:
            await asyncio.sleep(1)

    program._predictor = _Slow()  # type: ignore[attr-defined]
    result = asyncio.run(program.decide(_manifest()))
    assert result.decision.decision == "no_trade"
    assert result.trace["error"] == "deadline"


def test_a_malformed_answer_is_a_no_trade() -> None:
    program, _ = _program(_Prediction(**{**_ANSWER, "surprise": "not-a-number"}))
    result = asyncio.run(program.decide(_manifest()))
    assert result.decision.decision == "no_trade"
    assert result.trace["error"].startswith("schema:")


def test_an_answer_outside_the_enumeration_is_a_no_trade() -> None:
    program, _ = _program(_Prediction(**{**_ANSWER, "alignment": "maybe"}))
    result = asyncio.run(program.decide(_manifest()))
    assert result.decision.decision == "no_trade"


def test_the_package_never_builds_its_own_language_model() -> None:
    """The endpoint is composed by `tracefold.app` via `configured_lm_endpoint` and injected.

    A bare `dspy.LM` here lost the LiteLLM provider prefix and the qwen/deepseek thinking switches, so
    a model name spelled the way `news_triage_model` is spelled failed resolution and every News case
    degraded to a no-trade — after the daily budget had been charged.
    """

    import tracefold.trading.decision.program as module

    assert not hasattr(TradingDecisionProgram, "build")
    text = Path(module.__file__ or "").read_text(encoding="utf-8")
    assert "dspy.LM(" not in text


def test_the_program_holds_no_tools_and_no_agent_framework() -> None:
    import tracefold.trading.decision.program as module

    source = module.__file__
    assert source is not None
    text = Path(source).read_text(encoding="utf-8")
    for banned in ("deepagents", "langgraph", "langchain", "create_deep_agent", "dspy.ReAct", "dspy.Tool"):
        assert banned not in text, banned
