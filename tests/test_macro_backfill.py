from __future__ import annotations

from datetime import date

from tracefold.app.cli.parser import build_parser
from tracefold.macro.backfill import professional_backfill_policies
from tracefold.macro.module_payloads import _current_health, _dataset_states, _history_depth
from tracefold.macro.registry import DATASET_REGISTRY


def test_professional_backfill_policy_matches_confirmed_history_boundaries() -> None:
    through_date = date(2026, 7, 27)
    policies = {policy.dataset_id: policy for policy in professional_backfill_policies(through_date=through_date)}

    five_year_history = {
        "treasury.daily_nominal_curve",
        "treasury.daily_real_curve",
        "federal_reserve.fomc.documents",
        "federal_reserve.board.speeches",
        "federal_reserve.reserve_bank.speeches",
        "nasdaq.spy.daily",
        "nasdaq.qqq.daily",
        "yfinance.es_future.daily",
        "yfinance.hg_future.daily",
    }
    assert all(policies[dataset_id].start_date == date(2021, 7, 27) for dataset_id in five_year_history)
    assert all(policies[dataset_id].history_class == "trailing_five_years" for dataset_id in five_year_history)
    assert policies["fred.dcoilwtico"].start_date == date(1986, 1, 1)
    assert policies["cftc.tff.credit_positions"].start_date == date(2006, 6, 13)

    expected_intraday_proxies = {
        "yfinance.spy.intraday",
        "yfinance.qqq.intraday",
        "yfinance.iwm.intraday",
        "yfinance.tlt.intraday",
        "yfinance.ief.intraday",
        "yfinance.lqd.intraday",
        "yfinance.hyg.intraday",
        "yfinance.dxy.intraday",
        "yfinance.gld.intraday",
        "yfinance.uso.intraday",
    }
    assert expected_intraday_proxies.isdisjoint(policies)
    assert expected_intraday_proxies <= DATASET_REGISTRY.keys()


def test_wti_daily_observations_use_the_official_weekly_release_freshness() -> None:
    spec = DATASET_REGISTRY["fred.dcoilwtico"]

    assert spec.frequency == "daily"
    assert spec.refresh_seconds == 21_600
    assert spec.freshness_seconds == 950_400
    assert spec.critical is True


def test_professional_backfill_five_year_boundary_handles_leap_day() -> None:
    policies = {policy.dataset_id: policy for policy in professional_backfill_policies(through_date=date(2024, 2, 29))}

    assert policies["federal_reserve.fomc.documents"].start_date == date(2019, 2, 28)


def test_professional_backfill_includes_every_public_credit_series_and_no_placeholders() -> None:
    policies = {policy.dataset_id: policy for policy in professional_backfill_policies(through_date=date(2026, 7, 27))}
    public_credit_series = {
        spec.dataset_id
        for spec in DATASET_REGISTRY.values()
        if spec.module_id == "credit" and spec.fact_family == "series" and spec.adapter_id != "unavailable"
    }

    assert public_credit_series <= policies.keys()
    assert {
        "fred.drtscilm",
        "fred.drsdcilm",
        "fred.sublpdrcsn",
        "fred.sublpdrcdn",
        "fred.drtsclcc",
        "fred.demcc",
    } <= policies.keys()
    assert "licensed.credit.trace_nav" not in policies
    assert "federal_reserve.document.analysis" not in policies
    assert all(policy.start_date <= date(2026, 7, 27) for policy in policies.values())
    assert all(policy.priority == 75 for policy in policies.values() if policy.history_class.startswith("optional_"))


def test_macro_cli_exposes_one_professional_backfill_command() -> None:
    args = build_parser().parse_args(["macro", "backfill-professional"])

    assert args.command == "macro"
    assert args.macro_command == "backfill-professional"


def test_history_backfill_is_visible_but_never_becomes_a_judgment_gate() -> None:
    now_ms = 1_785_139_200_000
    spec = DATASET_REGISTRY["nasdaq.spy.daily"]
    latest_target = {
        "dataset_id": spec.dataset_id,
        "partition_key": "latest",
        "clock_kind": spec.clock_kind,
        "status": "current",
    }
    fact = {
        "dataset_id": spec.dataset_id,
        "reference_date": date(2026, 7, 27),
        "received_at_ms": now_ms,
    }

    backfill_target = {
        "dataset_id": spec.dataset_id,
        "partition_key": "2021-07-27..2026-07-27",
        "clock_kind": "backfill",
        "status": "backfilling",
        "cursor_json": {"history_class": "trailing_five_years"},
    }
    state = _dataset_states(
        (spec,),
        [latest_target, backfill_target],
        [fact],
        now_ms,
        analysis_job_state=None,
        backfill_worker_enabled=True,
    )[0]

    assert state["current_health"] == "current"
    assert state["current_reason"]["code"] == "within_freshness_budget"
    assert state["history_depth"] == "partial"
    assert state["required_for_history"] is False
    assert state["history_reason"]["code"] == "optional_maximum_history_incomplete"
    assert state["history_reason"]["impact"] == "none"


def test_terminal_best_effort_proxy_stays_local_and_does_not_degrade_current_health() -> None:
    now_ms = 1_785_139_200_000
    spec = DATASET_REGISTRY["yfinance.spy.intraday"]
    state = _dataset_states(
        (spec,),
        [
            {
                "dataset_id": spec.dataset_id,
                "partition_key": "latest",
                "clock_kind": spec.clock_kind,
                "status": "stale",
                "next_due_at_ms": now_ms + 300_000,
                "last_error_code": "provider_exhausted",
            }
        ],
        [],
        now_ms,
        analysis_job_state=None,
        backfill_worker_enabled=False,
    )[0]

    assert state["current_health"] == "unavailable"
    assert state["current_reason"]["code"] == "source_terminal_stale"
    assert state["current_reason"]["recovery"] == "operator_action"
    assert state["current_reason"]["next_check_at_ms"] is None
    assert state["required_for_current"] is False
    assert _current_health([state])["state"] == "current"


def test_optional_maximum_public_history_is_audit_only() -> None:
    now_ms = 1_785_139_200_000
    spec = DATASET_REGISTRY["nasdaq.spy.daily"]
    fact = {
        "dataset_id": spec.dataset_id,
        "observation_id": "spy-history-fact",
        "observed_at_ms": now_ms - 86_400_000,
        "published_at_ms": now_ms - 86_400_000,
        "received_at_ms": now_ms - 1_000,
        "value_numeric": 635.0,
        "unit": spec.unit,
        "source_url": spec.source_url,
    }
    states = _dataset_states(
        (spec,),
        [
            {
                "dataset_id": spec.dataset_id,
                "partition_key": "latest",
                "clock_kind": spec.clock_kind,
                "status": "current",
                "next_due_at_ms": now_ms + 86_400_000,
            },
            {
                "dataset_id": spec.dataset_id,
                "partition_key": "2021-07-29..2026-07-29",
                "clock_kind": "backfill",
                "status": "backfilling",
                "cursor_json": {"history_class": "maximum_public"},
                "next_due_at_ms": now_ms + 60_000,
            },
        ],
        [fact],
        now_ms,
        analysis_job_state=None,
        backfill_worker_enabled=True,
    )
    state = states[0]

    assert state["required_for_history"] is False
    assert state["history_depth"] == "partial"
    assert state["history_reason"]["code"] == "optional_maximum_history_incomplete"
    assert state["history_reason"]["impact"] == "none"
    assert _history_depth(states)["state"] == "not_required"
