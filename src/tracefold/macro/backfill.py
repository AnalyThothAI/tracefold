from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from tracefold.macro.registry import DATASET_REGISTRY

PROFESSIONAL_FIVE_YEAR_DATASETS = frozenset(
    {
        "treasury.daily_nominal_curve",
        "treasury.daily_real_curve",
        "federal_reserve.fomc.documents",
        "federal_reserve.board.speeches",
        "federal_reserve.reserve_bank.speeches",
        "binance.btcusdt.spot",
        "nasdaq.spy.daily",
        "nasdaq.qqq.daily",
        "nasdaq.iwm.daily",
        "nasdaq.tlt.daily",
        "nasdaq.ief.daily",
        "nasdaq.lqd.daily",
        "nasdaq.hyg.daily",
        "nasdaq.dxy.daily",
        "nasdaq.gld.daily",
        "nasdaq.uso.daily",
        "yfinance.es_future.daily",
        "yfinance.nq_future.daily",
        "yfinance.rty_future.daily",
        "yfinance.zb_future.daily",
        "yfinance.zn_future.daily",
        "yfinance.dx_future.daily",
        "yfinance.gc_future.daily",
        "yfinance.cl_future.daily",
        "yfinance.hg_future.daily",
    }
)


@dataclass(frozen=True, slots=True)
class MacroBackfillPolicy:
    dataset_id: str
    history_class: str
    start_date: date
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
        priority: int,
    ) -> None:
        spec = DATASET_REGISTRY[dataset_id]
        if spec.adapter_id == "unavailable" or spec.adapter_id.startswith("derived_"):
            return
        policies[dataset_id] = MacroBackfillPolicy(
            dataset_id=dataset_id,
            history_class=history_class,
            start_date=min(start_date, through_date),
            priority=priority,
        )

    recent_history_start = _years_before(through_date, 5)
    for dataset_id in PROFESSIONAL_FIVE_YEAR_DATASETS:
        register(
            dataset_id,
            "trailing_five_years",
            recent_history_start,
            priority=25,
        )

    for spec in DATASET_REGISTRY.values():
        if spec.module_id == "credit" and spec.fact_family == "series":
            register(
                spec.dataset_id,
                "optional_maximum_public_history",
                date(1900, 1, 1),
                priority=75,
            )

    register(
        "fred.dcoilwtico",
        "optional_maximum_public_history",
        date(1986, 1, 1),
        priority=75,
    )

    for spec in DATASET_REGISTRY.values():
        if spec.adapter_id == "cftc_tff":
            register(
                spec.dataset_id,
                "optional_full_tff_history",
                date(2006, 6, 13),
                priority=75,
            )

    return tuple(policies[key] for key in sorted(policies))


__all__ = [
    "PROFESSIONAL_FIVE_YEAR_DATASETS",
    "MacroBackfillPolicy",
    "professional_backfill_policies",
]
