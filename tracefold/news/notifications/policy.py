"""Pure notification policy over facts, reader state, and independent model evidence."""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Final, Literal

from ..updates.contracts import Claim, ClaimFields, ContentKind, EventUpdate
from ..updates.identity import identity
from .contracts import ClaimReason, ReaderPolicyScores, ReaderSnapshot
from .novelty import ReaderNovelty, Render
from .reader import NONE, AnchorEvidence, ReaderBackend, ReaderJudgment, ReportKind

# An ordinary push reaches the reader within three hours of the claim first being visible; a correction of
# something the reader was told is still worth it for twelve.
SOURCE_MAX_AGE_MS: Final = 3 * 60 * 60_000
CORRECTION_MAX_AGE_MS: Final = 12 * 60 * 60_000
# A roundup or a background paragraph can arrive fresh and still report what happened long ago: such a claim
# is not pushed once the day it names is more than a week before it first became visible (#742 PR-4).
OCCURRENCE_MAX_AGE_DAYS: Final = 7
# How long an adopted update waits for a reader judgment nobody can give before its claims are recorded as
# unassessed. They are never pushed on a guess.
READER_WAIT_MAX_MS: Final = 10 * 60_000
# The owner's one exception (#675 §7), narrowed by #742: a same-day price move this large is itself the fact,
# but only where a whole market moved and only as a move, never a level, a year-on-year figure or a rate.
PRICE_MOVE_EXCEPTION_PERCENT: Final = Decimal(5)
PRICE_MOVE_EXCEPTION_MARKETS: Final[frozenset[str]] = frozenset({"commodity", "index"})
_PERCENT_UNITS: Final[frozenset[str]] = frozenset({"%", "pct", "percent"})
_MOVE_WORDS: Final = re.compile(r"change|move|rise|fall|drop|gain|loss|decline|jump|plunge|surge|涨|跌|升|降", re.I)
_SAME_DAY_WORDS: Final = re.compile(r"intraday|today|daily|session|日内|当日|今日|今天|盘中|收盘", re.I)
# A bare month dates an event; on a figure it names the statistical period, not when anything happened.
_OCCURRENCE_KINDS: Final[frozenset[ContentKind]] = frozenset({"state_change", "official_measure", "other"})
_ISO_DATE: Final = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")
_MONTH: Final = re.compile(r"^(?:early |mid-|late )?([a-z]+)\.?,?\s*(\d{4})?$")
_MONTHS: Final = (
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
)


@dataclass(frozen=True, slots=True)
class ReaderCalibration:
    """Reviewed logistic parameters and independently certified cuts for one backend.

    The zero coefficients are an explicitly unfitted placeholder. They never
    authorize a push; real reasks and independent owner labels are required
    before replacing them and marking this backend certified.
    """

    materiality_floor: int = 2
    push_coefficients: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    key_coefficients: tuple[float, float, float] = (0.0, 0.0, 0.0)
    push_cut: float | None = None
    key_cut: float | None = None
    # A claim is anchored to its most likely message when P(none) is below this. An increment is written
    # against the anchored message; a linked increment without an anchor is written in full.
    anchor_none_below: float = 0.2
    certification_status: Literal["uncalibrated", "certified"] = "uncalibrated"

    def __post_init__(self) -> None:
        if self.materiality_floor not in {1, 2, 3}:
            raise ValueError("news_reader_materiality_floor_invalid")
        if (
            len(self.push_coefficients) != 4
            or len(self.key_coefficients) != 3
            or not all(math.isfinite(value) for value in (*self.push_coefficients, *self.key_coefficients))
        ):
            raise ValueError("news_reader_calibration_coefficients_invalid")
        if not 0 < self.anchor_none_below < 1 or any(
            cut is not None and not 0 <= cut <= 1 for cut in (self.push_cut, self.key_cut)
        ):
            raise ValueError("news_reader_calibration_cut_invalid")
        if self.certification_status not in {"uncalibrated", "certified"} or (
            self.certification_status == "certified" and (self.push_cut is None or self.key_cut is None)
        ):
            raise ValueError("news_reader_calibration_certification_invalid")


PUSHABLE_KINDS: Final[dict[ReportKind, bool]] = {
    "new_action": True,
    "official_communication": True,
    "market_move": True,
    "scheduled_data": True,
    "self_reported_metric": True,
    "unconfirmed_incident": True,
    "recap_or_old_period": False,
    "promotion": False,
    "commentary": False,
    "background": False,
}
KIND_FLOOR: Final = 0.3
LOGIT_EPSILON: Final = 1e-6

READER_CALIBRATIONS: Final[dict[ReaderBackend, ReaderCalibration]] = {
    "native": ReaderCalibration(),
    "generated": ReaderCalibration(),
}


NOTIFICATION_POLICY_IDENTITY: Final = identity(
    "news_notification_policy",
    "report_kind_materiality_interrupt_v1",
    PUSHABLE_KINDS,
    KIND_FLOOR,
    LOGIT_EPSILON,
    {backend: asdict(calibration) for backend, calibration in READER_CALIBRATIONS.items()},
    SOURCE_MAX_AGE_MS,
    CORRECTION_MAX_AGE_MS,
    OCCURRENCE_MAX_AGE_DAYS,
    READER_WAIT_MAX_MS,
)


def calibration_for(judgment: ReaderJudgment) -> ReaderCalibration:
    """Use the independently calibrated backend that actually answered."""

    if judgment.backend is None:
        raise ValueError("news_reader_judgment_unavailable")
    return READER_CALIBRATIONS[judgment.backend]


def anchor_index(evidence: AnchorEvidence, calibration: ReaderCalibration) -> int | None:
    """Select a core-fact anchor from model evidence under this policy."""

    if evidence.probabilities[NONE] >= calibration.anchor_none_below:
        return None
    best = max(
        (value for value in evidence.probabilities if value != NONE),
        key=evidence.probabilities.__getitem__,
    )
    return int(best[1:]) - 1


ReaderOutcome = Literal["known", "in_flight", "correction", "key", "push", "feed"]


@dataclass(frozen=True, slots=True)
class ReaderDecision:
    outcome: ReaderOutcome
    render: Render
    # The earlier receipt the card and the record name: the delivered claim a development changes, else the
    # message the anchor says already reported the claim's core fact.
    anchor_intent_id: str | None = None
    scores: ReaderPolicyScores | None = None


def _logit(probability: float) -> float:
    clipped = min(1 - LOGIT_EPSILON, max(LOGIT_EPSILON, probability))
    return math.log(clipped) - math.log1p(-clipped)


def _sigmoid(value: float) -> float:
    if value >= 0:
        return 1 / (1 + math.exp(-value))
    exponential = math.exp(value)
    return exponential / (1 + exponential)


def reader_scores(
    judgment: ReaderJudgment, *, held: bool = False, calibration: ReaderCalibration | None = None
) -> ReaderPolicyScores:
    """Replay stored distributions without changing the model or its cache identity.

    Neither the materiality expectation nor the anchor probability participates
    in these probabilities. The incremental materiality question already
    compares all supplied messages.
    """

    if judgment.report_kind is None or judgment.materiality is None or judgment.interrupt is None:
        raise ValueError("news_reader_judgment_unavailable")
    calibration = calibration or calibration_for(judgment)
    # Validated distributions tolerate provider rounding at 1e-6. Keep the raw
    # evidence intact while ensuring a summed probability is still in [0, 1].
    e = min(
        1.0,
        sum(probability for kind, probability in judgment.report_kind.probabilities.items() if PUSHABLE_KINDS[kind]),
    )
    m = min(1.0, sum(judgment.materiality.probabilities[calibration.materiality_floor :]))
    i = judgment.interrupt.probability
    a, b1, b2, b3 = calibration.push_coefficients
    c, d, f = calibration.key_coefficients
    return ReaderPolicyScores(
        e=e,
        m=m,
        i=i,
        p_push=_sigmoid(a + b1 * _logit(e) + b2 * _logit(m) + b3 * held),
        p_key=_sigmoid(c + d * _logit(i) + f * _logit(e)),
        held=held,
        certification_status=calibration.certification_status,
    )


def novelty_outcome(novelty: ReaderNovelty, *, first_available_at_ms: int) -> ReaderDecision | None:
    """The reader rows that need no judgment: known, in flight, and the correction of a delivered claim.

    A correction is repaired regardless of its score, but only when it became visible after the delivery it
    corrects; an older report that merely disagrees with a later one is not a correction of what was sent.
    """

    if novelty.novelty == "known":
        return ReaderDecision("known", "full", novelty.intent_id)
    if novelty.novelty == "in_flight":
        return ReaderDecision("in_flight", "full", novelty.intent_id)
    relation = next((link.relation for link in novelty.path if link.relation != "equivalent"), None)
    if (
        novelty.novelty == "development"
        and relation == "corrects"
        and first_available_at_ms > (novelty.settled_at_ms or first_available_at_ms)
    ):
        return ReaderDecision("correction", "correction", novelty.intent_id)
    return None


def reader_decision(
    novelty: ReaderNovelty,
    judgment: ReaderJudgment,
    *,
    first_available_at_ms: int,
    message_intents: Sequence[str],
    calibration: ReaderCalibration | None = None,
    claim_fields: ClaimFields | None = None,
) -> ReaderDecision:
    """The reader rows of the decision table, in order, for one claim with an available judgment.

    Known and in-flight claims are never pushed, and a later correction of a delivered claim always is
    (`novelty_outcome`). Everything else, a real-world development of a delivered claim included, is pushed
    on its eligible type mass and calibrated incremental materiality. Held is
    one logistic input, never a separate cut. An unanchored effective state change
    is scored as an ordinary new fact: a background link
    does not make an actual new action a detail. A development is written against the claim it changes; anything
    else is written as an increment only on the message the anchor names, since a link alone may join
    different facts of one story. `message_intents` are the receipts behind `ReaderInput.messages`.
    """

    decided = novelty_outcome(novelty, first_available_at_ms=first_available_at_ms)
    if decided is not None:
        return decided
    if judgment.status != "available":
        raise ValueError("news_reader_judgment_unavailable")
    calibration = calibration or calibration_for(judgment)
    if novelty.novelty == "development":
        anchor = novelty.intent_id
    else:
        index = None if judgment.anchor is None else anchor_index(judgment.anchor, calibration)
        if index is not None and index >= len(message_intents):
            raise ValueError("news_reader_anchor_message_missing")
        anchor = None if index is None else message_intents[index]
    # A semantic information link can join an incident to its later recovery,
    # or a plan to actual execution. An unanchored effective state change is
    # scored as its own action. Unknown phase, promises, parameters and mere
    # corroboration retain the existing increment hurdle.
    effective_action = (
        claim_fields is not None
        and claim_fields.content_kind in {"state_change", "official_measure"}
        and claim_fields.mode in {"observation", "decision"}
        and claim_fields.phase in {"ordered", "effective", "executing", "completed", "cancelled"}
    )
    held = (novelty.novelty == "increment" and not (anchor is None and effective_action)) or (
        novelty.novelty == "unlinked" and anchor is not None
    )
    scores = reader_scores(judgment, held=held, calibration=calibration)
    pushed = (
        calibration.certification_status == "certified"
        and scores.e >= KIND_FLOOR
        and calibration.push_cut is not None
        and scores.p_push >= calibration.push_cut
    )
    key = pushed and calibration.key_cut is not None and scores.p_key >= calibration.key_cut
    outcome: ReaderOutcome = "feed" if not pushed else ("key" if key else "push")
    return ReaderDecision(outcome, "full" if anchor is None else "increment", anchor, scores)


def large_daily_move(claim: Claim) -> bool:
    """A same-day percentage move of at least the exception size on a commodity or index primary.

    Only a moved level (`level_crossed`) counts, only a quantity named as a change, and only a same-day or
    unstated period: a yield level, a year-on-year figure or a crop-condition rate is not a daily move.
    """

    if claim.fields.content_kind != "level_crossed":
        return False
    markets = {asset.market_type for asset in claim.fields.assets if asset.role == "primary"}
    if not markets & PRICE_MOVE_EXCEPTION_MARKETS:
        return False
    for quantity in claim.fields.quantities:
        if quantity.unit.strip().casefold() not in _PERCENT_UNITS or not _MOVE_WORDS.search(quantity.name):
            continue
        period = quantity.period or claim.fields.statistical_period
        if period is not None and not _SAME_DAY_WORDS.search(period):
            continue
        try:
            if abs(Decimal(quantity.value)) >= PRICE_MOVE_EXCEPTION_PERCENT:
                return True
        except InvalidOperation:
            continue
    return False


def _occurred_on(claim: Claim, seen: date) -> date | None:
    """The latest day the claim's `occurred_at` can name, read conservatively; None when it names none.

    The extractor invents years, so an ISO date keeps its year only when a cited quote states it and otherwise
    takes the year that puts it nearest `seen`, the day the claim first became visible. A bare month, with an
    optional early / mid- / late, names its last day, within the past year unless a quote states its year; it
    counts only for an event (`_OCCURRENCE_KINDS`). Anything else, "Sept. 10" included, names no day.
    """

    text = (claim.fields.occurred_at or "").strip().lower()
    quotes = " ".join(citation.quote for citation in claim.citations)
    if iso := _ISO_DATE.match(text):
        days = []
        for year in (int(iso[1]),) if iso[1] in quotes else (seen.year - 1, seen.year, seen.year + 1):
            try:
                days.append(date(year, int(iso[2]), int(iso[3])))
            except ValueError:
                continue
        return min(days, key=lambda day: abs((day - seen).days), default=None)
    named = _MONTH.match(text)
    if named is None or len(named[1]) < 3 or claim.fields.content_kind not in _OCCURRENCE_KINDS:
        return None
    month = next((index for index, name in enumerate(_MONTHS, 1) if name.startswith(named[1])), None)
    if month is None:
        return None
    year = seen.year - 1 if month > seen.month else seen.year
    if named[2] and named[2] in quotes:
        year = int(named[2])
    following = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return date.fromordinal(following.toordinal() - 1)


def stale_occurrence(claim: Claim) -> bool:
    """Whether the day the claim reports is more than `OCCURRENCE_MAX_AGE_DAYS` before it first became visible.

    A claim with a speaker is the statement itself, a new act whatever it recounts, so it is never stale here.
    """

    if claim.fields.speaker:
        return False
    seen = datetime.fromtimestamp(claim.first_available_at_ms / 1000, UTC).date()
    day = _occurred_on(claim, seen)
    return day is not None and (seen - day).days > OCCURRENCE_MAX_AGE_DAYS


READER_REASONS: Final[dict[str, ClaimReason]] = {
    "known": "known_to_reader",
    "in_flight": "linked_send_in_flight",
    "correction": "correction_of_sent",
    "key": "reader_key",
    "push": "reader_push",
    "feed": "reader_feed",
}


def decide(
    claim: Claim,
    update: EventUpdate,
    reader: ReaderSnapshot,
    *,
    now_ms: int,
    novelty: ReaderNovelty,
    judgment: ReaderJudgment | None,
    message_intents: Sequence[str] = (),
) -> tuple[ClaimReason, ReaderDecision | None]:
    """The decision table for one claim, in order. Pure: every input is already read.

    `judgment` is None when the claim was not asked; it is asked only when no earlier row decides it.
    """

    age = now_ms - claim.first_available_at_ms
    corrects = novelty.novelty == "development" and any(link.relation == "corrects" for link in novelty.path)
    corrective = corrects or any(
        change.current_ref == claim.ref and change.kind in {"correction", "conflict"} for change in update.changes
    )
    if claim.ref in set(update.retired_claim_refs) | set(update.superseded_claim_refs) | set(
        reader.invalidated_claim_refs
    ):
        return "retired", None
    if claim.ref in reader.blocked_claim_refs:
        return "send_outcome_unresolved", None
    if claim.ref in reader.ambiguous_claim_refs:
        return "send_outcome_ambiguous", None
    if age > (CORRECTION_MAX_AGE_MS if corrective else SOURCE_MAX_AGE_MS):
        return "stale_source", None
    if not corrective and stale_occurrence(claim):
        return "stale_occurrence", None
    early = novelty_outcome(novelty, first_available_at_ms=claim.first_available_at_ms)
    if early is not None:
        return READER_REASONS[early.outcome], early
    if claim.ref in reader.protected_listing_claim_refs:
        return "protected_listing", None
    if claim.fields.mode == "observation" and large_daily_move(claim):
        return "large_daily_move", None
    if judgment is None or judgment.status != "available":
        waited = now_ms - update.adopted_at_ms > READER_WAIT_MAX_MS
        return ("reader_unassessed" if waited else "reader_unavailable"), None
    judged = reader_decision(
        novelty,
        judgment,
        first_available_at_ms=claim.first_available_at_ms,
        message_intents=message_intents,
        claim_fields=claim.fields,
    )
    return READER_REASONS[judged.outcome], judged
