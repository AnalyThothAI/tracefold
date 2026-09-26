"""One reader selection owner: explicit content rules, stable intents and actual delivered-text coverage.

The planner reads adopted EventUpdate content and the reader's actual receipts. Every claim gets one named
decision. Content rules are explicit and ordered; there is no statement drop, headline-similarity veto,
same-story count, ticker requirement or importance score. The only reason a plan stays pending is an
overlapping send whose outcome is not settled.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Final, Literal, Protocol

from pydantic import Field, model_validator

from .contracts import Claim, ContentKind, EventUpdate, Exact, Mode
from .identity import canonical_json, digest, identity
from .judgment import Budget, NewsJudgments, Question
from .topics import KEY_TOPIC_CODES

SOURCE_MAX_AGE_MS: Final = 12 * 60 * 60_000

PlanAction = Literal["notify", "no_notification", "unresolved"]
PlanReason = Literal["uncovered_claims", "send_outcome_unresolved", "no_uncovered_actionable_claims"]
ClaimDecisionValue = Literal["notify", "not_notified", "deferred"]
ClaimReason = Literal[
    # notify: the admission path of a claim no sent receipt fully covers
    "watchlist_hit",
    "large_daily_move",
    "actionable_content",
    # not_notified
    "retired",
    "mode_commentary",
    "mode_promotion",
    "mode_forecast",
    "mode_unknown",
    "content_schedule",
    "price_report_without_basis",
    "stale_source",
    "covered_by_sent_receipt",
    # deferred: an overlapping sending/ambiguous intent must settle first
    "send_outcome_unresolved",
]
REASON_DECISIONS: Final[dict[ClaimReason, ClaimDecisionValue]] = {
    "watchlist_hit": "notify",
    "large_daily_move": "notify",
    "actionable_content": "notify",
    "retired": "not_notified",
    "mode_commentary": "not_notified",
    "mode_promotion": "not_notified",
    "mode_forecast": "not_notified",
    "mode_unknown": "not_notified",
    "content_schedule": "not_notified",
    "price_report_without_basis": "not_notified",
    "stale_source": "not_notified",
    "covered_by_sent_receipt": "not_notified",
    "send_outcome_unresolved": "deferred",
}
_MODE_REASONS: Final[dict[Mode, ClaimReason]] = {
    "commentary": "mode_commentary",
    "promotion": "mode_promotion",
    "forecast": "mode_forecast",
}
# Answers a mode re-ask may settle on. `unknown` is deliberately absent.
_KNOWN_MODES: Final[dict[str, Mode]] = {
    "observation": "observation",
    "decision": "decision",
    "commitment": "commitment",
    "conditional_threat": "conditional_threat",
    "guidance": "guidance",
    "forecast": "forecast",
    "commentary": "commentary",
    "promotion": "promotion",
}
# The kinds whose whole claim is a market move. An observed one must state a basis beyond the number.
PRICE_REPORT_KINDS: Final[frozenset[ContentKind]] = frozenset({"level_crossed", "period_record", "quantified_flow"})
# The owner's one exception (#675 §7): a same-day move this large is itself the fact, but only where a whole
# market moved. A single stock is excluded by its market, never by the size of the move.
PRICE_MOVE_EXCEPTION_PERCENT: Final = Decimal(5)
PRICE_MOVE_EXCEPTION_MARKETS: Final[frozenset[str]] = frozenset({"commodity", "index"})
_PERCENT_UNITS: Final[frozenset[str]] = frozenset({"%", "pct", "percent"})
# A key update is a state change or an authority's measure in a key topic family, corroborated.
KEY_CONTENT_KINDS: Final[frozenset[ContentKind]] = frozenset({"state_change", "official_measure"})
KEY_MIN_INDEPENDENT_ORIGINS: Final = 2


class DeliveredText(Exact):
    intent_id: str
    channel: str
    state: Literal["sent", "not_sent", "ambiguous"]
    body: str
    payload_sha256: str
    received_at_ms: int | None = Field(default=None, ge=0)
    provider_message_id: str | None = None

    @model_validator(mode="after")
    def actual_receipt(self) -> DeliveredText:
        if self.payload_sha256 != digest(self.body):
            raise ValueError("news_receipt_payload_mismatch")
        if self.state == "sent" and self.received_at_ms is None:
            raise ValueError("news_receipt_sent_time_missing")
        return self


class ReaderSnapshot(Exact):
    channel: str
    revision: str
    # Retrieved receipt rows, never observed event heads or unsent drafts.
    receipts: tuple[DeliveredText, ...]
    # Claims of overlapping sending/ambiguous intents. Never counted as received.
    blocked_claim_refs: tuple[str, ...] = ()
    # The reader's code-owned watchlist, as canonical upper-case base symbols supplied by the store.
    watch_symbols: tuple[str, ...] = ()


class ClaimDecision(Exact):
    claim_ref: str
    decision: ClaimDecisionValue
    reason: ClaimReason

    @model_validator(mode="after")
    def check_reason(self) -> ClaimDecision:
        if REASON_DECISIONS[self.reason] != self.decision:
            raise ValueError("news_claim_decision_reason_mismatch")
        return self


class NotificationPlan(Exact):
    action: PlanAction
    reason: PlanReason
    update_ref: str
    # One decision per adopted claim, so the Console can show why each was or was not sent.
    claim_decisions: tuple[ClaimDecision, ...]
    # Replaces the retired escalate class: a louder presentation of this notification, never a gate.
    key: bool = False
    channel: str
    purpose: Literal["news_update"] = "news_update"
    reader_revision: str

    @property
    def selected_claim_refs(self) -> tuple[str, ...]:
        return tuple(sorted(row.claim_ref for row in self.claim_decisions if row.decision == "notify"))

    @property
    def deferred_claim_refs(self) -> tuple[str, ...]:
        return tuple(sorted(row.claim_ref for row in self.claim_decisions if row.decision == "deferred"))

    @property
    def intent_id(self) -> str:
        if self.action != "notify" or not self.selected_claim_refs:
            raise ValueError("news_non_notification_has_no_intent")
        return identity("intent", self.update_ref, sorted(self.selected_claim_refs), self.channel, self.purpose)

    @model_validator(mode="after")
    def check_action(self) -> NotificationPlan:
        refs = [row.claim_ref for row in self.claim_decisions]
        if len(refs) != len(set(refs)):
            raise ValueError("news_plan_duplicate_claim_decision")
        if self.selected_claim_refs:
            expected = ("notify", "uncovered_claims")
        elif self.deferred_claim_refs:
            expected = ("unresolved", "send_outcome_unresolved")
        else:
            expected = ("no_notification", "no_uncovered_actionable_claims")
        if (self.action, self.reason) != expected:
            raise ValueError("news_plan_action_mismatch")
        if self.key and self.action != "notify":
            raise ValueError("news_plan_key_without_notification")
        return self


class CardLine(Exact):
    claim_ref: str
    text_zh: str = Field(min_length=1)


class CardCopy(Exact):
    headline_zh: str = Field(min_length=1, max_length=80)
    lines: tuple[CardLine, ...] = Field(min_length=1)


class FrozenCard(Exact):
    intent_id: str
    claim_refs: tuple[str, ...]
    headline_zh: str
    body: str
    payload_sha256: str

    @model_validator(mode="after")
    def check_payload(self) -> FrozenCard:
        if self.payload_sha256 != digest(self.body):
            raise ValueError("news_card_payload_mismatch")
        return self


class CardComposer(Protocol):
    async def compose(self, claims: tuple[Claim, ...]) -> CardCopy:
        """Chinese copy for exactly the selected claims. The caller bounds the call with asyncio.timeout."""
        ...


def market_move(claim: Claim, mode: Mode) -> bool:
    """An observed market move: the class whose basis the planner asks about.

    A decision, commitment or guidance that carries an amount (a Treasury buyback, a rate path) is an
    action, not a quote wearing a level.
    """

    return mode == "observation" and claim.fields.content_kind in PRICE_REPORT_KINDS


def content_reason(claim: Claim, mode: Mode) -> ClaimReason:
    """The admission reason for a claim of a known mode that is not a market move, or why it is not notified.

    Rules in order: a commentary, promotion or forecast mode; a schedule. A market move is decided by
    `market_reason` from its judged basis.
    """

    reason = _MODE_REASONS.get(mode)
    if reason is not None:
        return reason
    if claim.fields.content_kind == "schedule":
        return "content_schedule"
    return "actionable_content"


def large_daily_move(claim: Claim) -> bool:
    """A structured percentage move of at least the exception size on a commodity or index primary."""

    markets = {asset.market_type for asset in claim.fields.assets if asset.role == "primary"}
    if not markets & PRICE_MOVE_EXCEPTION_MARKETS:
        return False
    for quantity in claim.fields.quantities:
        if quantity.unit.strip().casefold() not in _PERCENT_UNITS:
            continue
        try:
            if abs(Decimal(quantity.value)) >= PRICE_MOVE_EXCEPTION_PERCENT:
                return True
        except InvalidOperation:
            continue
    return False


def market_reason(claim: Claim, basis: str | None) -> ClaimReason:
    """A market move with a stated basis is notified; a bare quote only under the owner's exception.

    An unresolved basis is content uncertainty, not a verdict: it does not withhold the claim.
    """

    if basis == "quote_only":
        return "large_daily_move" if large_daily_move(claim) else "price_report_without_basis"
    return "actionable_content"


def watchlist_hit(claim: Claim, watch_symbols: frozenset[str]) -> bool:
    return any(
        asset.role == "primary" and asset.symbol.strip().upper() in watch_symbols for asset in claim.fields.assets
    )


def corroborated(claim: Claim, update: EventUpdate) -> bool:
    """Two supporting sources of distinct origin, or one supporting source of a named authority.

    Origin falls back to the publisher when ingestion did not know it. Reports and copies do not count:
    only a `supports` relationship is corroboration.
    """

    evidence = {item.ref: item for item in update.evidence}
    supporting = [
        evidence[row.evidence_ref].source
        for row in update.evidence_relations
        if row.claim_ref == claim.ref and row.relation == "supports"
    ]
    if any(source.source_authority != "unknown" for source in supporting):
        return True
    origins = {source.origin_id or source.publisher_id for source in supporting}
    return len(origins) >= KEY_MIN_INDEPENDENT_ORIGINS


def is_key(claim: Claim, update: EventUpdate) -> bool:
    if claim.fields.content_kind not in KEY_CONTENT_KINDS:
        return False
    if not set(update.topics) & KEY_TOPIC_CODES:
        return False
    return corroborated(claim, update)


class NotificationPlanner:
    def __init__(self, judgments: NewsJudgments, *, source_max_age_ms: int = SOURCE_MAX_AGE_MS) -> None:
        self.judgments = judgments
        self.source_max_age_ms = source_max_age_ms

    async def plan(
        self,
        update: EventUpdate,
        reader: ReaderSnapshot,
        budget: Budget,
        *,
        now_ms: int,
    ) -> NotificationPlan:
        """One named decision per claim, in rule order.

        Retired claims; the watchlist guard; mode (an unknown mode gets one generated re-ask, then
        `mode_unknown`); schedule; a market move without a stated basis; a stale source (corrections and
        conflicts exempt); an unsettled overlapping send (deferred); full coverage by an actually sent
        receipt. A claim no rule removed is notified.
        """

        decisions: dict[str, ClaimReason] = {}
        admitted: dict[str, ClaimReason] = {}
        retired = set(update.retired_claim_refs)
        watch = frozenset(symbol.strip().upper() for symbol in reader.watch_symbols)
        # Explicit corrections must not inherit a TTL refreshed by a model run.
        # They can nevertheless inform readers about an old report: not an entry signal.
        corrections = {change.current_ref for change in update.changes if change.kind in {"correction", "conflict"}}

        def stale(claim: Claim) -> bool:
            too_old = now_ms - claim.first_available_at_ms > self.source_max_age_ms
            return self.source_max_age_ms > 0 and too_old and claim.ref not in corrections

        unknown_mode: list[Claim] = []
        market: list[Claim] = []
        for claim in update.claims:
            if claim.ref in retired:
                decisions[claim.ref] = "retired"
            elif watchlist_hit(claim, watch):
                # The objective guard: a reader's own asset is a candidate whatever its mode or content,
                # still subject to staleness and to what the reader actually received.
                admitted[claim.ref] = "watchlist_hit"
            elif claim.fields.mode == "unknown" and stale(claim):
                # No model call can rescue a stale source; do not pay for a re-ask.
                decisions[claim.ref] = "stale_source"
            elif claim.fields.mode == "unknown":
                unknown_mode.append(claim)
            elif market_move(claim, claim.fields.mode):
                market.append(claim)
            else:
                self._admit(claim, content_reason(claim, claim.fields.mode), decisions, admitted)
        for claim, mode in await self._reask_modes(update, tuple(unknown_mode), budget):
            if mode == "unknown":
                decisions[claim.ref] = "mode_unknown"
            elif market_move(claim, mode):
                market.append(claim)
            else:
                self._admit(claim, content_reason(claim, mode), decisions, admitted)
        bases = await self._market_bases(update, tuple(claim for claim in market if not stale(claim)), budget)
        for claim in market:
            if stale(claim):
                decisions[claim.ref] = "stale_source"
            else:
                self._admit(claim, market_reason(claim, bases.get(claim.ref)), decisions, admitted)

        by_ref = {claim.ref: claim for claim in update.claims}
        blocked = set(reader.blocked_claim_refs)
        coverage_candidates: list[Claim] = []
        for ref in admitted:
            claim = by_ref[ref]
            if stale(claim):
                decisions[ref] = "stale_source"
            elif ref in blocked:
                # A new intent must not blindly repeat an unsettled external send; not received either.
                decisions[ref] = "send_outcome_unresolved"
            else:
                coverage_candidates.append(claim)
        covered = await self._fully_covered(tuple(coverage_candidates), reader, budget)
        for claim in coverage_candidates:
            decisions[claim.ref] = "covered_by_sent_receipt" if claim.ref in covered else admitted[claim.ref]

        rows = tuple(
            ClaimDecision(
                claim_ref=claim.ref,
                decision=REASON_DECISIONS[decisions[claim.ref]],
                reason=decisions[claim.ref],
            )
            for claim in update.claims
        )
        selected = [by_ref[row.claim_ref] for row in rows if row.decision == "notify"]
        action: PlanAction
        reason: PlanReason
        if selected:
            action = "notify"
            reason = "uncovered_claims"
        elif any(row.decision == "deferred" for row in rows):
            action = "unresolved"
            reason = "send_outcome_unresolved"
        else:
            action = "no_notification"
            reason = "no_uncovered_actionable_claims"
        return NotificationPlan(
            action=action,
            reason=reason,
            update_ref=update.ref,
            claim_decisions=rows,
            key=any(is_key(claim, update) for claim in selected),
            channel=reader.channel,
            reader_revision=reader.revision,
        )

    @staticmethod
    def _admit(
        claim: Claim,
        reason: ClaimReason,
        decisions: dict[str, ClaimReason],
        admitted: dict[str, ClaimReason],
    ) -> None:
        if REASON_DECISIONS[reason] == "notify":
            admitted[claim.ref] = reason
        else:
            decisions[claim.ref] = reason

    def _cited_questions(self, update: EventUpdate, claims: tuple[Claim, ...]) -> tuple[Question, ...]:
        evidence = {item.ref: item for item in update.evidence}
        return tuple(
            Question(
                item_id=claim.ref,
                payload_json=canonical_json(
                    {
                        "claim": claim,
                        "evidence": [evidence[citation.evidence_ref] for citation in claim.citations],
                    }
                ),
            )
            for claim in claims
        )

    async def _market_bases(
        self,
        update: EventUpdate,
        claims: tuple[Claim, ...],
        budget: Budget,
    ) -> dict[str, str | None]:
        """What each observed market move states beyond its number, asked once for all of them.

        The configured judgment backend answers (a native Choice when Jev is configured). An unresolved or
        unavailable answer gets one targeted generated re-ask; what stays unresolved is `None`.
        """

        if not claims:
            return {}
        questions = self._cited_questions(update, claims)
        answers = {row.item_id: row for row in await self.judgments.judge("market_basis", questions, budget)}

        def settled(ref: str) -> str | None:
            row = answers.get(ref)
            if row is None or row.status != "available" or row.value in {None, "unresolved"}:
                return None
            return str(row.value)

        bases = {claim.ref: settled(claim.ref) for claim in claims}
        pending = tuple(question for question in questions if bases[question.item_id] is None)
        if pending:
            answers = {row.item_id: row for row in await self.judgments.reask("market_basis", pending, budget)}
            bases.update({question.item_id: settled(question.item_id) for question in pending})
        return bases

    async def _reask_modes(
        self,
        update: EventUpdate,
        claims: tuple[Claim, ...],
        budget: Budget,
    ) -> list[tuple[Claim, Mode]]:
        """One targeted generated re-ask of `mode` for exactly the claims that stayed unknown.

        An answer that is still unknown or unavailable is recorded as `mode_unknown`, so the plan completes
        rather than retrying content uncertainty for ever.
        """

        if not claims:
            return []
        answers = await self.judgments.reask("mode", self._cited_questions(update, claims), budget)
        modes: list[tuple[Claim, Mode]] = []
        for claim, answer in zip(claims, answers, strict=True):
            mode: Mode = "unknown"
            if answer.status == "available" and answer.value in _KNOWN_MODES:
                mode = _KNOWN_MODES[str(answer.value)]
            modes.append((claim, mode))
        return modes

    async def _fully_covered(
        self,
        claims: tuple[Claim, ...],
        reader: ReaderSnapshot,
        budget: Budget,
    ) -> set[str]:
        """Claims an actually sent receipt on this channel fully covers.

        Partial, unresolved and unavailable answers are not full coverage. Unsent, not-sent and ambiguous
        copy is never reader coverage.
        """

        sent = tuple(row for row in reader.receipts if row.state == "sent" and row.channel == reader.channel)
        pairs: dict[str, str] = {}
        questions = []
        for claim in claims:
            for receipt in sent:
                item_id = identity("coverage", claim.ref, receipt.intent_id, receipt.payload_sha256)
                pairs[item_id] = claim.ref
                payload = {
                    "claim": claim,
                    "actual_delivered_text": receipt.body,
                    "receipt_id": receipt.intent_id,
                    "payload_sha256": receipt.payload_sha256,
                }
                questions.append(Question(item_id=item_id, payload_json=canonical_json(payload)))
        if not questions:
            return set()
        answers = await self.judgments.judge("coverage", tuple(questions), budget)
        return {pairs[row.item_id] for row in answers if row.status == "available" and row.value == "full"}


def _has_han(text: str) -> bool:
    return any("㐀" <= char <= "鿿" for char in text)


# Reader copy is plain text that every channel shows exactly as frozen. A link or a control character
# in model copy is not something a channel may strip afterwards, so such copy is refused, not cleaned.
_COPY_LINK_RE: Final = re.compile(r"https?://|www\.", re.IGNORECASE)
_COPY_CONTROL_RE: Final = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def _unsafe_copy(text: str) -> bool:
    return bool(_COPY_LINK_RE.search(text) or _COPY_CONTROL_RE.search(text))


def freeze_card(plan: NotificationPlan, update: EventUpdate, copy: CardCopy) -> FrozenCard:
    """Freeze actual reader copy; selected IDs are not proof of full coverage.

    Future coverage decisions compare this exact delivered body, not the original
    article, the selected-ID set, or an unsent draft. Adapters may reject oversized
    copy, but may not silently truncate the frozen body.
    """
    refs = plan.selected_claim_refs
    if plan.update_ref != update.ref or not set(refs) <= {claim.ref for claim in update.claims}:
        raise ValueError("news_card_update_selection_mismatch")
    if {line.claim_ref for line in copy.lines} != set(refs) or len(copy.lines) != len(refs):
        raise ValueError("news_card_selected_claims_mismatch")
    lines = {line.claim_ref: line.text_zh for line in copy.lines}
    if not _has_han(copy.headline_zh) or any(not _has_han(text) for text in lines.values()):
        raise ValueError("news_card_chinese_copy_required")
    if "\n" in copy.headline_zh or any(_unsafe_copy(text) for text in (copy.headline_zh, *lines.values())):
        raise ValueError("news_card_copy_unsafe")
    body = "\n\n".join([copy.headline_zh, *(lines[ref] for ref in refs)])
    return FrozenCard(
        intent_id=plan.intent_id,
        claim_refs=refs,
        headline_zh=copy.headline_zh,
        body=body,
        payload_sha256=digest(body),
    )
