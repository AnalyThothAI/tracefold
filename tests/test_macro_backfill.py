from __future__ import annotations

from datetime import date

from tracefold.app.cli.parser import build_parser
from tracefold.macro.backfill import professional_backfill_policies
from tracefold.macro.module_payloads import _dataset_states
from tracefold.macro.registry import DATASET_REGISTRY


def test_professional_backfill_policy_matches_confirmed_history_boundaries() -> None:
    through_date = date(2026, 7, 27)
    policies = {policy.dataset_id: policy for policy in professional_backfill_policies(through_date=through_date)}

    required_recent_history = {
        "treasury.daily_nominal_curve",
        "treasury.daily_real_curve",
        "federal_reserve.fomc.documents",
        "federal_reserve.board.speeches",
        "federal_reserve.reserve_bank.speeches",
    }
    assert all(policies[dataset_id].start_date == date(2021, 7, 27) for dataset_id in required_recent_history)
    assert all(
        policies[dataset_id].history_class == "required_trailing_five_years" for dataset_id in required_recent_history
    )
    assert policies["fred.dcoilwtico"].start_date == date(1986, 1, 1)
    assert policies["fred.dcoilwtico"].required_for_judgment is False
    assert policies["cftc.tff.credit_positions"].start_date == date(2006, 6, 13)
    assert policies["cftc.tff.credit_positions"].required_for_judgment is False

    expected_etfs = {
        "nasdaq.spy.history",
        "nasdaq.qqq.history",
        "nasdaq.iwm.history",
        "nasdaq.tlt.history",
        "nasdaq.ief.history",
        "nasdaq.lqd.history",
        "nasdaq.hyg.history",
        "nasdaq.dxy.history",
        "nasdaq.gld.history",
        "nasdaq.uso.history",
    }
    assert expected_etfs.isdisjoint(policies)
    assert expected_etfs <= DATASET_REGISTRY.keys()


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
    assert all(
        not policy.required_for_judgment for policy in policies.values() if policy.history_class.startswith("optional_")
    )
    assert all(policy.priority == 75 for policy in policies.values() if policy.history_class.startswith("optional_"))


def test_macro_cli_exposes_one_professional_backfill_command() -> None:
    args = build_parser().parse_args(["macro", "backfill-professional"])

    assert args.command == "macro"
    assert args.macro_command == "backfill-professional"


def test_only_readiness_required_backfills_block_module_health() -> None:
    now_ms = 1_785_139_200_000
    spec = DATASET_REGISTRY["treasury.daily_nominal_curve"]
    latest_target = {
        "dataset_id": spec.dataset_id,
        "partition_key": "latest",
        "clock_kind": "official_state",
        "status": "current",
    }
    fact = {
        "dataset_id": spec.dataset_id,
        "reference_date": date(2026, 7, 27),
        "received_at_ms": now_ms,
    }

    def state_for(required_for_judgment: bool) -> dict[str, object]:
        backfill_target = {
            "dataset_id": spec.dataset_id,
            "partition_key": "2021-07-27..2026-07-27",
            "clock_kind": "backfill",
            "status": "backfilling",
            "cursor_json": {"required_for_judgment": required_for_judgment},
        }
        return _dataset_states(
            (spec,),
            [latest_target, backfill_target],
            [fact],
            now_ms,
            analysis_job_state=None,
        )[0]

    assert state_for(False)["state"] == "current"
    required_state = state_for(True)
    assert required_state["state"] == "backfilling"
    assert required_state["reason"] == "required_history_backfill_incomplete"
