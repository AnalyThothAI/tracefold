"""An equivalent answer is vetoed only by comparable evidence, not wording differences."""

from __future__ import annotations

from tests.support.news_update_semantic import update_one
from tracefold.news.notifications.reader import ReaderInput
from tracefold.news.updates.assembly import proven_mismatches
from tracefold.news.updates.contracts import Change


def pair():
    _, extraction, head = update_one()
    return extraction.claims[0], head.claims[0]


def altered(current, **fields):
    return current.model_copy(update={"fields": current.fields.model_copy(update=fields)})


def test_free_text_period_condition_and_partial_time_are_unknown() -> None:
    current, previous = pair()
    previous = previous.model_copy(
        update={
            "fields": previous.fields.model_copy(
                update={
                    "conditions": ("over a seven-week low",),
                    "statistical_period": "past 24 hours",
                    "occurred_at": "early Monday",
                }
            )
        }
    )
    candidate = altered(
        current,
        conditions=("over a 7-week low",),
        statistical_period="last 24 hours",
        occurred_at="Monday",
    )
    assert proven_mismatches(candidate, previous) == ()


def test_explicit_quarter_and_absolute_time_conflicts_are_proven() -> None:
    current, previous = pair()
    previous = previous.model_copy(
        update={
            "fields": previous.fields.model_copy(
                update={
                    "statistical_period": "Q4 2025",
                    "effective_at": "2026-10-01",
                    "occurred_at": "2026-09-29T10:00:00Z",
                }
            )
        }
    )
    candidate = altered(
        current,
        statistical_period="2026 Q1",
        effective_at="2026-10-02",
        occurred_at="2026-09-29T11:00:00+00:00",
    )
    assert proven_mismatches(candidate, previous) == ("effective_at", "occurred_at", "statistical_period")


def test_quantity_only_compares_exact_metric_unit_and_aligned_period() -> None:
    current, previous = pair()
    different_value = altered(current, quantities=(current.fields.quantities[0].model_copy(update={"value": "50"}),))
    assert proven_mismatches(different_value, previous) == ("quantity",)
    different_metric = altered(
        different_value, quantities=(different_value.fields.quantities[0].model_copy(update={"name": "other measure"}),)
    )
    assert proven_mismatches(different_metric, previous) == ()
    rolling = altered(
        different_value,
        quantities=(different_value.fields.quantities[0].model_copy(update={"period": "last 24 hours"}),),
    )
    assert proven_mismatches(rolling, previous) == ()


def test_quantity_veto_does_not_depend_on_free_text_subject_or_object() -> None:
    """Nvidia and Nvidia Corp are one issuer: a different buyback size is still a proven difference."""

    current, previous = pair()

    def quantity(name: str = "buyback", value: str = "150", unit: str = "billion USD", period: str | None = None):
        return (
            current.fields.quantities[0].model_copy(
                update={"name": name, "value": value, "unit": unit, "period": period}
            ),
        )

    previous = previous.model_copy(
        update={
            "fields": previous.fields.model_copy(
                update={
                    "subject": "Nvidia Corp",
                    "object": "",
                    "statistical_period": "Q3 2026",
                    "quantities": quantity(period="Q3 2026"),
                }
            )
        }
    )
    nvidia = altered(current, subject="Nvidia", object="its own shares", statistical_period="2026 Q3")
    assert proven_mismatches(
        altered(nvidia, quantities=quantity("Buyback ", "60", " Billion USD", "2026Q3")), previous
    ) == ("quantity",)
    assert proven_mismatches(altered(nvidia, quantities=quantity(value="150.0", period="Q3 2026")), previous) == ()
    assert proven_mismatches(altered(nvidia, quantities=quantity("dividend", "60", period="Q3 2026")), previous) == ()
    rolling = altered(nvidia, statistical_period="trailing twelve months", quantities=quantity(value="60"))
    assert proven_mismatches(rolling, previous) == ()
    unparsed = altered(nvidia, quantities=quantity(value="60", period="through fiscal 2028"))
    assert proven_mismatches(unparsed, previous) == ()


def test_clear_polarity_mode_and_phase_conflicts_remain_protected() -> None:
    current, previous = pair()
    previous = previous.model_copy(update={"fields": previous.fields.model_copy(update={"polarity": "affirmative"})})
    candidate = altered(current, polarity="negative", mode="observation", phase="completed")
    assert proven_mismatches(candidate, previous) == ("mode", "phase", "polarity")


def test_reader_input_does_not_depend_on_unreferenced_change_order() -> None:
    _, _, update = update_one()
    claim = update.claims[0]
    extra = Change(kind="scope_change", current_ref=claim.ref, previous_ref="unread", previous_content_ref="update:old")
    forward = update.model_copy(update={"changes": (*update.changes, extra)})
    reverse = update.model_copy(update={"changes": tuple(reversed(forward.changes))})
    left = ReaderInput.of(claim, forward, ())
    right = ReaderInput.of(claim, reverse, ())
    assert left == right and left.digest == right.digest
    assert "change" not in left.model_inputs()["claim"]
