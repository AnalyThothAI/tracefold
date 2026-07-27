from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from tracefold.macro.registry import DATASET_REGISTRY


@dataclass(frozen=True, slots=True)
class MacroBackfillPolicy:
    dataset_id: str
    history_class: str
    start_date: date
    required_for_judgment: bool
    priority: int


def _years_before(value: date, years: int) -> date:
    try:
        return value.replace(year=value.year - years)
    except ValueError:
        return value.replace(year=value.year - years, day=28)


def professional_backfill_policies(*, through_date: date) -> tuple[MacroBackfillPolicy, ...]:
    policies: dict[str, MacroBackfillPolicy] = {}

    def register(
        dataset_id: str,
        history_class: str,
        start_date: date,
        *,
        required_for_judgment: bool,
    ) -> None:
        spec = DATASET_REGISTRY[dataset_id]
        if spec.adapter_id == "unavailable" or spec.adapter_id.startswith("derived_"):
            return
        policies[dataset_id] = MacroBackfillPolicy(
            dataset_id=dataset_id,
            history_class=history_class,
            start_date=min(start_date, through_date),
            required_for_judgment=required_for_judgment,
            priority=25 if required_for_judgment else 75,
        )

    recent_history_start = _years_before(through_date, 5)
    for dataset_id in (
        "treasury.daily_nominal_curve",
        "treasury.daily_real_curve",
        "federal_reserve.fomc.documents",
        "federal_reserve.board.speeches",
        "federal_reserve.reserve_bank.speeches",
    ):
        register(
            dataset_id,
            "required_trailing_five_years",
            recent_history_start,
            required_for_judgment=True,
        )

    for spec in DATASET_REGISTRY.values():
        if spec.module_id == "credit" and spec.fact_family == "series":
            register(
                spec.dataset_id,
                "optional_maximum_public_history",
                date(1900, 1, 1),
                required_for_judgment=False,
            )

    register(
        "fred.dcoilwtico",
        "optional_maximum_public_history",
        date(1986, 1, 1),
        required_for_judgment=False,
    )

    for dataset_id in (
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
        "binance.btcusdt.spot",
    ):
        register(
            dataset_id,
            "required_trailing_five_years",
            recent_history_start,
            required_for_judgment=True,
        )

    for spec in DATASET_REGISTRY.values():
        if spec.adapter_id == "cftc_tff":
            register(
                spec.dataset_id,
                "optional_full_tff_history",
                date(2006, 6, 13),
                required_for_judgment=False,
            )

    return tuple(policies[key] for key in sorted(policies))


__all__ = ["MacroBackfillPolicy", "professional_backfill_policies"]
