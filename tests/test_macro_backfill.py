from __future__ import annotations

from datetime import date

from tracefold.app.cli.parser import build_parser
from tracefold.macro.backfill import professional_backfill_policies
from tracefold.macro.registry import DATASET_REGISTRY


def test_professional_backfill_policy_matches_confirmed_history_boundaries() -> None:
    through_date = date(2026, 7, 27)
    policies = {policy.dataset_id: policy for policy in professional_backfill_policies(through_date=through_date)}

    assert policies["treasury.daily_nominal_curve"].start_date == date(2000, 1, 1)
    assert policies["treasury.daily_real_curve"].start_date == date(2000, 1, 1)
    assert policies["federal_reserve.fomc.documents"].start_date == date(2000, 1, 1)
    assert policies["federal_reserve.board.speeches"].history_class == ("official_trailing_ten_years")
    assert policies["federal_reserve.reserve_bank.speeches"].start_date <= date(2016, 7, 27)
    assert policies["fred.dcoilwtico"].start_date == date(1986, 1, 1)
    assert policies["cftc.tff.credit_positions"].start_date == date(2006, 6, 13)

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
    assert expected_etfs <= policies.keys()
    assert all(policies[dataset_id].history_class == "trailing_five_years" for dataset_id in expected_etfs)


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


def test_macro_cli_exposes_one_professional_backfill_command() -> None:
    args = build_parser().parse_args(["macro", "backfill-professional"])

    assert args.command == "macro"
    assert args.macro_command == "backfill-professional"
