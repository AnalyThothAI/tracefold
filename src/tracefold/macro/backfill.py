from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from tracefold.macro.registry import DATASET_REGISTRY


@dataclass(frozen=True, slots=True)
class MacroBackfillPolicy:
    dataset_id: str
    history_class: str
    start_date: date


def professional_backfill_policies(*, through_date: date) -> tuple[MacroBackfillPolicy, ...]:
    policies: dict[str, MacroBackfillPolicy] = {}

    def register(dataset_id: str, history_class: str, start_date: date) -> None:
        spec = DATASET_REGISTRY[dataset_id]
        if spec.adapter_id == "unavailable" or spec.adapter_id.startswith("derived_"):
            return
        policies[dataset_id] = MacroBackfillPolicy(
            dataset_id=dataset_id,
            history_class=history_class,
            start_date=min(start_date, through_date),
        )

    for dataset_id in (
        "treasury.daily_nominal_curve",
        "treasury.daily_real_curve",
        "federal_reserve.fomc.documents",
    ):
        register(dataset_id, "official_since_2000", date(2000, 1, 1))

    speech_start = through_date - timedelta(days=10 * 366)
    for dataset_id in (
        "federal_reserve.board.speeches",
        "federal_reserve.reserve_bank.speeches",
    ):
        register(dataset_id, "official_trailing_ten_years", speech_start)

    for spec in DATASET_REGISTRY.values():
        if spec.module_id == "credit" and spec.fact_family == "series":
            register(spec.dataset_id, "maximum_public_history", date(1900, 1, 1))

    register("fred.dcoilwtico", "maximum_public_history", date(1986, 1, 1))

    proxy_start = through_date - timedelta(days=5 * 366)
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
        register(dataset_id, "trailing_five_years", proxy_start)

    for spec in DATASET_REGISTRY.values():
        if spec.adapter_id == "cftc_tff":
            register(spec.dataset_id, "full_tff_history", date(2006, 6, 13))

    return tuple(policies[key] for key in sorted(policies))


__all__ = ["MacroBackfillPolicy", "professional_backfill_policies"]
