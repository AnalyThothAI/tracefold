"""V3 publication, scoped plan ownership and last Trading validity check.

OI research is superseded by a newer metric revision; News catalyst research only by a newer
catalyst that names one of its claims as replaced, and a correction refuses the entry instead.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Any

import pytest
from psycopg.errors import UniqueViolation

from tests.integration.test_trading_analysis_storage import _selection
from tests.postgres_test_utils import connect_postgres_test
from tests.postgres_test_utils import reset_postgres_schema as migrate
from tests.trading.news_public_updates import first_report, next_update
from tracefold.news.updates.contracts import PublicUpdate
from tracefold.platform.market_identity import DEFAULT_UNIVERSE
from tracefold.trading.engine.policy import decision_identity
from tracefold.trading.execution_contracts import (
    SignalEntryEnvelopeV3,
    SignalExitPlanV1,
    TradeSignalV3,
    market_key,
)
from tracefold.trading.storage.execution_stream import prepare_trade_signal_v3
from tracefold.trading.storage.root import TradingRepository
from tracefold.trading.storage.trade_plans import prepare_trade_plan
from tracefold.trading.trade_plan import TradePlan

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("postgres_migration_dsn")]


def test_signal_v3_scope_and_pre_submit_check(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "signal-v3-db", read_only=False)
    try:
        migrate(conn)
        trading = TradingRepository(conn)
        payload = {
            "source_recorded_at_ms": 1_000,
            "provider_event_at_ms": 900,
            "assets": [{"symbol": "SOL", "market_type": "crypto", "role": "primary"}],
        }
        with conn.transaction():
            _, case_id, _ = trading.accept_trigger(
                kind="oi",
                source_fact_key="frame-1",
                source_revision="oi-v1",
                payload_sha256="a" * 64,
                payload=payload,
                selection=_selection(),
                now_ms=1_100,
                root_ttl_ms=10_000,
            )
            case = trading.claim_analysis_case(now_ms=1_500, lease_ms=5_000)
        assert case is not None
        decision = {
            "decision_version": "trade_decision_v4",
            "action": "TRADE",
            "selected_plan_id": "e" * 64,
            "side": "short",
            "reason": "frozen fixture",
            "exit_plan": {"stop_distance_bps": 150, "take_profit_bps": 300, "max_holding_seconds": 3_600},
        }
        decision_id = decision_identity(case_id, decision)
        signal = TradeSignalV3(
            seq=1,
            signal_id="b" * 64,
            case_id=case_id,
            decision_id=decision_id,
            account_slot="binance_usdm_primary",
            entry_scope_id=case["entry_scope_id"],
            asset_id="crypto:SOL",
            market_key=market_key("SOL"),
            native_symbol="SOLUSDT",
            mapping_semantics_digest=case["mapping_semantics_digest"],
            direction="short",
            observed_at_ns=2_000_000_000,
            expires_at_ns=6_000_000_000,
            exit_plan=SignalExitPlanV1(stop_distance_bps=150, take_profit_bps=300, max_holding_ns=3_600_000_000_000),
            entry_envelope=SignalEntryEnvelopeV3(
                plan_id="e" * 64,
                entry_kind="immediate_entry_v1",
                root_expires_at_ns=11_000_000_000,
                reference_price=Decimal("100"),
                max_price_drift_bps=200,
                universe_version=DEFAULT_UNIVERSE.digest,
            ),
        )
        with conn.transaction():
            assert trading.finish_analysis_case(
                case_id=case_id,
                claim_token=case["claim_token"],
                now_ms=2_000,
                analysis_status="analyzed",
                evidence_ref="evidence",
                decision=decision,
                assessment_ref="assessment",
                prepared_signal=prepare_trade_signal_v3(signal),
            )
        assert trading.unresolved_trade_signals(
            account_slot="binance_usdm_primary",
            execution_strategy="oi_nautilus_v1",
            now_ns=2_100_000_000,
            limit=10,
        ) == ((1, signal.model_dump(mode="json", exclude={"seq"})),)
        assert (
            trading.unresolved_trade_signals(
                account_slot="other_connection",
                execution_strategy="oi_nautilus_v1",
                now_ns=2_100_000_000,
                limit=10,
            )
            == ()
        )
        plan = TradePlan(
            entry_id=signal.signal_id,
            entry_scope_id=signal.entry_scope_id,
            source="signal",
            case_id=case_id,
            account_slot=signal.account_slot,
            market_key=signal.market_key,
            instrument_id="SOLUSDT-PERP.BINANCE",
            direction="short",
            entry_client_order_id="tf" + "b" * 30,
            created_at_ns=2_200_000_000,
            entry_expires_at_ns=signal.expires_at_ns,
            entry_quantity=Decimal("1"),
            stop_distance_bps=150,
            risk_budget_usd=Decimal("10"),
            max_leverage_at_creation=2,
            exit_policy_id="analysis_dynamic_v1",
            take_profit_bps=300,
            max_holding_ns=3_600_000_000_000,
            updated_at_ns=2_200_000_000,
        )
        with conn.transaction():
            assert trading.insert_trade_plan(prepare_trade_plan(plan))
            assert trading.validate_signal_entry(entry_id=plan.entry_id, now_ns=2_500_000_000) == (True, "valid")
        rival = plan.model_copy(update={"entry_id": "c" * 64, "entry_client_order_id": "tf" + "c" * 30})
        with pytest.raises(UniqueViolation), conn.transaction():
            trading.insert_trade_plan(prepare_trade_plan(rival))
        with conn.transaction():
            _, revised_case_id, _ = trading.accept_trigger(
                kind="oi",
                source_fact_key="frame-1",
                source_revision="oi-v2",
                payload_sha256="d" * 64,
                payload=payload | {"source_recorded_at_ms": 2_550},
                selection=_selection(),
                now_ms=2_600,
                root_ttl_ms=10_000,
            )
            revised = conn.execute(
                "SELECT entry_scope_id FROM trading_cases WHERE case_id=%s",
                (revised_case_id,),
            ).fetchone()
            assert revised["entry_scope_id"] == signal.entry_scope_id
            assert trading.validate_signal_entry(entry_id=plan.entry_id, now_ns=2_700_000_000) == (
                False,
                "source_superseded",
            )
    finally:
        conn.close()


def _publish_entry(trading: TradingRepository, case: dict[str, Any], *, now_ms: int) -> TradePlan:
    """Settle one claimed Case with a published TRADE signal and its unsubmitted entry plan."""

    decision = {
        "decision_version": "trade_decision_v4",
        "action": "TRADE",
        "selected_plan_id": "e" * 64,
        "side": "long",
        "reason": "frozen fixture",
        "exit_plan": {"stop_distance_bps": 150, "take_profit_bps": 300, "max_holding_seconds": 3_600},
    }
    signal = TradeSignalV3(
        seq=1,
        signal_id="b" * 64,
        case_id=case["case_id"],
        decision_id=decision_identity(case["case_id"], decision),
        account_slot="binance_usdm_primary",
        entry_scope_id=case["entry_scope_id"],
        asset_id="crypto:SOL",
        market_key=market_key("SOL"),
        native_symbol="SOLUSDT",
        mapping_semantics_digest=case["mapping_semantics_digest"],
        direction="long",
        observed_at_ns=now_ms * 1_000_000,
        expires_at_ns=(now_ms + 5_000) * 1_000_000,
        exit_plan=SignalExitPlanV1(stop_distance_bps=150, take_profit_bps=300, max_holding_ns=3_600_000_000_000),
        entry_envelope=SignalEntryEnvelopeV3(
            plan_id="e" * 64,
            entry_kind="immediate_entry_v1",
            root_expires_at_ns=int(case["root_expires_at_ms"]) * 1_000_000,
            reference_price=Decimal("100"),
            max_price_drift_bps=200,
            universe_version=DEFAULT_UNIVERSE.digest,
        ),
    )
    assert trading.finish_analysis_case(
        case_id=case["case_id"],
        claim_token=case["claim_token"],
        now_ms=now_ms,
        analysis_status="analyzed",
        evidence_ref="evidence",
        decision=decision,
        assessment_ref="assessment",
        prepared_signal=prepare_trade_signal_v3(signal),
    )
    plan = TradePlan(
        entry_id=signal.signal_id,
        entry_scope_id=signal.entry_scope_id,
        source="signal",
        case_id=case["case_id"],
        account_slot=signal.account_slot,
        market_key=signal.market_key,
        instrument_id="SOLUSDT-PERP.BINANCE",
        direction="long",
        entry_client_order_id="tf" + "b" * 30,
        created_at_ns=(now_ms + 100) * 1_000_000,
        entry_expires_at_ns=signal.expires_at_ns,
        entry_quantity=Decimal("1"),
        stop_distance_bps=150,
        risk_budget_usd=Decimal("10"),
        max_leverage_at_creation=2,
        exit_policy_id="analysis_dynamic_v1",
        take_profit_bps=300,
        max_holding_ns=3_600_000_000_000,
        updated_at_ns=(now_ms + 100) * 1_000_000,
    )
    assert trading.insert_trade_plan(prepare_trade_plan(plan))
    return plan


def _accept(trading: TradingRepository, update: PublicUpdate, *, now_ms: int) -> tuple[str, str, str]:
    payload = update.model_dump(mode="json")
    return trading.accept_trigger(
        kind="catalyst",
        source_fact_key=update.event_id,
        source_revision=update.content_revision,
        payload_sha256=hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest(),
        payload=payload,
        selection=_selection(),
        now_ms=now_ms,
        root_ttl_ms=10_000,
    )


def test_catalyst_supersession_is_claim_scoped(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "catalyst-scope-db", read_only=False)
    try:
        migrate(conn)
        trading = TradingRepository(conn)
        head, first = first_report(event_id="event-sol", first_available_at_ms=1_000, completed_at_ms=1_050)
        with conn.transaction():
            first_trigger, _, _ = _accept(trading, first, now_ms=1_100)
            case = trading.claim_analysis_case(now_ms=1_500, lease_ms=5_000)
        assert case is not None
        # Information added to the same Event names no replaced claim: the earlier research stays valid.
        added_update, added = next_update(
            head,
            "SOL protocol exempts stablecoin pairs from the swap fee.",
            previous_ref=head.claims[0].ref,
            relation="adds_information",
            change_kind="scope_change",
            quantity="25",
            revision=2,
            first_available_at_ms=1_600,
            completed_at_ms=1_650,
        )
        assert added.superseded_claim_refs == ()
        with conn.transaction():
            added_trigger, added_case_id, disposition = _accept(trading, added, now_ms=1_700)
            assert disposition == "accepted"
            plan = _publish_entry(trading, case, now_ms=2_000)
            assert trading.validate_signal_entry(entry_id=plan.entry_id, now_ns=2_500_000_000) == (True, "valid")
        published = conn.execute(
            "SELECT publish_status,publish_reason FROM trading_case_decisions WHERE case_id=%s", (case["case_id"],)
        ).fetchone()
        assert published == {"publish_status": "published", "publish_reason": None}
        scope = conn.execute("SELECT entry_scope_id FROM trading_cases WHERE case_id=%s", (added_case_id,)).fetchone()
        assert scope["entry_scope_id"] == case["entry_scope_id"]
        # A real parameter change of the first claim supersedes only research that cited it.
        _, changed = next_update(
            added_update,
            "SOL protocol raises the swap fee to 40 bps.",
            previous_ref=head.claims[0].ref,
            relation="real_world_change",
            change_kind="parameter_change",
            quantity="40",
            revision=3,
            first_available_at_ms=2_600,
            completed_at_ms=2_650,
        )
        assert changed.superseded_claim_refs == first.claim_refs
        with conn.transaction():
            _accept(trading, changed, now_ms=2_700)
            assert trading.validate_signal_entry(entry_id=plan.entry_id, now_ns=2_800_000_000) == (
                False,
                "source_superseded",
            )
            assert trading._trigger_superseded(first_trigger, known_at_ms=2_800)
            assert not trading._trigger_superseded(added_trigger, known_at_ms=2_800)
            # The superseding fact is not visible to a decision settled before it was accepted.
            assert not trading._trigger_superseded(first_trigger, known_at_ms=2_699)
    finally:
        conn.close()


def test_research_settled_after_a_superseding_catalyst_is_not_published(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "catalyst-superseded-db", read_only=False)
    try:
        migrate(conn)
        trading = TradingRepository(conn)
        head, first = first_report(event_id="event-sol", first_available_at_ms=1_000, completed_at_ms=1_050)
        with conn.transaction():
            _accept(trading, first, now_ms=1_100)
            case = trading.claim_analysis_case(now_ms=1_500, lease_ms=5_000)
        assert case is not None
        _, changed = next_update(
            head,
            "SOL protocol raises the swap fee to 40 bps.",
            previous_ref=head.claims[0].ref,
            relation="real_world_change",
            change_kind="parameter_change",
            quantity="40",
            revision=2,
            first_available_at_ms=1_600,
            completed_at_ms=1_650,
        )
        with conn.transaction():
            _accept(trading, changed, now_ms=1_700)
            finished = trading.finish_analysis_case(
                case_id=case["case_id"],
                claim_token=case["claim_token"],
                now_ms=2_000,
                analysis_status="analyzed",
                evidence_ref="evidence",
                decision={
                    "decision_version": "trade_decision_v4",
                    "action": "TRADE",
                    "selected_plan_id": "e" * 64,
                    "side": "long",
                    "reason": "frozen fixture",
                },
            )
        assert finished
        assert conn.execute(
            "SELECT publish_status,publish_reason FROM trading_case_decisions WHERE case_id=%s", (case["case_id"],)
        ).fetchone() == {"publish_status": "superseded", "publish_reason": "source_superseded"}
    finally:
        conn.close()


def test_correction_refuses_the_unsubmitted_entry_without_a_trigger_or_fresh_ttl(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "catalyst-correction-db", read_only=False)
    try:
        migrate(conn)
        trading = TradingRepository(conn)
        head, first = first_report(event_id="event-sol", first_available_at_ms=1_000, completed_at_ms=1_050)
        with conn.transaction():
            _accept(trading, first, now_ms=1_100)
            case = trading.claim_analysis_case(now_ms=1_500, lease_ms=5_000)
            assert case is not None
            plan = _publish_entry(trading, case, now_ms=2_000)
        before = conn.execute("SELECT * FROM trading_cases ORDER BY case_id").fetchall()

        def receive(update: PublicUpdate, now_ms: int) -> str:
            payload = update.model_dump(mode="json")
            return trading.receive_source_update(
                update_id=update.update_id,
                source_fact_key=update.event_id,
                content_revision=update.content_revision,
                affected_claim_refs=update.affected_claim_refs,
                retired_claim_refs=update.retired_claim_refs,
                payload=payload,
                payload_sha256=hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest(),
                now_ms=now_ms,
            )

        # New support for the cited claim amends the research but retires nothing.
        _, confirmed = next_update(
            head,
            "Exchange notice confirms SOL protocol sets the swap fee to 25 bps.",
            previous_ref=head.claims[0].ref,
            relation="equivalent",
            change_kind=None,
            quantity="25",
            revision=2,
            first_available_at_ms=2_100,
            completed_at_ms=2_150,
        )
        assert confirmed.kind == "source_update" and confirmed.retired_claim_refs == ()
        with conn.transaction():
            assert receive(confirmed, 2_200) == "accepted"
            assert trading.validate_signal_entry(entry_id=plan.entry_id, now_ns=2_300_000_000) == (True, "valid")
        _, correction = next_update(
            head,
            "Correction: the SOL swap fee was set to 20 bps, not 25 bps.",
            previous_ref=head.claims[0].ref,
            relation="corrects",
            change_kind="correction",
            quantity="20",
            revision=3,
            first_available_at_ms=2_400,
            completed_at_ms=2_450,
        )
        with conn.transaction():
            assert receive(correction, 2_500) == "accepted"
            assert receive(correction, 2_600) == "duplicate"
            assert trading.validate_signal_entry(entry_id=plan.entry_id, now_ns=2_700_000_000) == (
                False,
                "source_corrected",
            )
        # Neither amendment created a trigger or Case, moved the root expiry or touched the plan.
        assert conn.execute("SELECT count(*) AS n FROM trading_triggers").fetchone()["n"] == 1
        assert conn.execute("SELECT * FROM trading_cases ORDER BY case_id").fetchall() == before
        assert conn.execute(
            "SELECT terminal_at_ns FROM trading_trade_plans WHERE entry_id=%s", (plan.entry_id,)
        ).fetchone() == {"terminal_at_ns": None}
    finally:
        conn.close()
