"""Atomic Item-to-Event admission and the broker admission consumer."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, ClassVar

from .. import liquidations, oi_signals
from ..bus import (
    Q_RAW,
    RK_EVENT,
    BusMessage,
    DeferError,
    PermanentError,
    TransientError,
    now_ms,
)
from ..events.facts import FactUnit, extract_fact_units
from ..events.gate import GateInput, GateVerdict, evaluate_gate
from ..events.identity import dedupe_family, dedupe_window_ms
from ..events.minhash import band_keys, minhash_signature
from ..events.storyline import preliminary_storyline_key
from ..events.titles import description_after_title, extract_title
from ..events.tokens import comparison_tokens, jaccard
from ..models import ADMITTED_ADMISSIONS, EVENT_IDENTITY_VERSION
from ..opennews import OPENNEWS_SOURCE_ID, OpenNewsEvent, parse_opennews_message
from ..source_contracts import (
    EventKind,
    SourceContract,
    SourceContractReason,
    classify_source_contract,
    classify_source_contracts,
    source_contract_admission,
)
from ..telemetry import NewsWorkSemantics
from .runtime import NewsDatabasePort

NEAR_DUPLICATE_THRESHOLD = 0.55
# How far back an exact *artifact* match may reach (#154). The dedupe-family windows bound how long two texts stay
# comparable; this bounds how long the platform's own primary key stays meaningful, which is much longer. The
# measured worst case in a 30-day window was a tweet the provider re-sent 88.7 h later under a new record id.
ARTIFACT_WINDOW_MS = 7 * 24 * 60 * 60_000
_TICKER_RE = re.compile(r"\$([A-Z]{2,6})\b")
_NUMBER_RE = re.compile(r"(\d[\d,]*\.?\d*)\s*(%|bn|billion|m|million|k|bps|tn|trillion)?", re.IGNORECASE)

# Admissions a stronger member may not overwrite. `telemetry_deterministic` is decided by the
# provider's strategy id, and upgrading it to `candidate` would route a fixed-format frame back into
# the model call the admission exists to avoid (#137).
_REGATE_ADMISSIONS = frozenset(
    {
        "candidate",
        "listing_deterministic",
        "telemetry_deterministic",
        "liquidation_deterministic",
        "unsupported_market_contract",
        "recovery",
    }
)
_STRONG_MEMBER_SCORE = 80.0

_INSTRUMENT_CACHE_TTL_MS = 10 * 60_000


@dataclass(frozen=True, slots=True)
class AdmitResult:
    item_id: str
    item_inserted: bool
    event_id: str
    event_created: bool
    admission: str
    event_kind: EventKind
    match_kind: str  # leader | exact | near | none
    gate: GateVerdict | None
    dedupe_family: str
    storyline_key: str
    comparison_fingerprint: str
    title: str
    evidence_focus_changed: bool = False


@dataclass(frozen=True, slots=True)
class AdmitBatchResult:
    """All deterministic FactUnits admitted from one provider Item."""

    item_id: str
    item_inserted: bool
    results: tuple[AdmitResult, ...]


def item_identity(*, source_id: str, source_item_key: str) -> str:
    return hashlib.sha256(f"{source_id}\x1f{source_item_key}".encode()).hexdigest()


def _event_identity(
    *,
    item_id: str,
    fact: FactUnit,
    event_kind: EventKind,
) -> str:
    return hashlib.sha256(
        f"{EVENT_IDENTITY_VERSION}\x1f{item_id}\x1f{fact.fact_id}\x1f{event_kind}".encode()
    ).hexdigest()


def admit_frame(
    repos: Any,
    *,
    event: OpenNewsEvent,
    ingest_mode: str,
    observed_at_ms: int,
    trace_id: str,
    watchlist_symbols: frozenset[str],
    now_ms: int,
    text_override: str | None = None,
    suppress_low_signal: bool = False,
    instrument_classes: Mapping[str, str] | None = None,
) -> AdmitBatchResult:
    """Admit every high-confidence FactUnit in one provider Item."""

    raw_text = text_override if text_override is not None else (event.raw_text or _reconstruct_text(event))
    parent = extract_title(raw_text)
    item_id = item_identity(source_id=OPENNEWS_SOURCE_ID, source_item_key=event.provider_record_id)
    units = extract_fact_units(
        item_id=item_id,
        raw_text=raw_text,
        fallback_title=parent.title or (event.entry.title or "")[:500],
    )
    contracts: list[SourceContract] = []
    seen_kinds: set[EventKind] = set()
    for contract in classify_source_contracts(event.provider_metadata):
        # Event identity is one provider FactUnit per kind. Keeping the first
        # durable tuple for a repeated kind makes a merged Item replay exactly
        # like the original first-seen delivery order.
        if contract.event_kind not in seen_kinds:
            seen_kinds.add(contract.event_kind)
            contracts.append(contract)
    results = tuple(
        admit_item(
            repos,
            event=event,
            ingest_mode=ingest_mode,
            observed_at_ms=observed_at_ms,
            trace_id=trace_id,
            watchlist_symbols=watchlist_symbols,
            now_ms=now_ms,
            text_override=text_override,
            suppress_low_signal=suppress_low_signal,
            instrument_classes=instrument_classes,
            _fact_unit=unit,
            _source_contract=contract,
        )
        for unit in units
        for contract in contracts
    )
    return AdmitBatchResult(item_id=item_id, item_inserted=any(r.item_inserted for r in results), results=results)


def _strong_facts(title: str, grounded: Sequence[str]) -> tuple[set[str], set[str]]:
    tickers = set(_TICKER_RE.findall(title)) | {g.upper().replace("XYZ-", "") for g in grounded}
    numbers = {
        m.group(0).replace(",", "").replace(" ", "").lower()
        for m in _NUMBER_RE.finditer(title)
        if len(m.group(1).replace(",", "")) >= 2
    }
    return tickers, numbers


def _compatible(a: tuple[set[str], set[str]], b: tuple[set[str], set[str]]) -> bool:
    if a[0] and b[0] and not (a[0] & b[0]):
        return False
    return not (a[1] and b[1] and not (a[1] & b[1]))


def _engine_type(metadata: Mapping[str, Any]) -> str:
    strategies = metadata.get("strategies") or []
    engine = ""
    if strategies and isinstance(strategies[0], Mapping):
        engine = str(strategies[0].get("engine_type") or "")
    return engine if engine in {"news", "meme", "listing", "market"} else "unknown"


def admit_item(
    repos: Any,
    *,
    event: OpenNewsEvent,
    ingest_mode: str,
    observed_at_ms: int,
    trace_id: str,
    watchlist_symbols: frozenset[str],
    now_ms: int,
    text_override: str | None = None,
    suppress_low_signal: bool = False,
    instrument_classes: Mapping[str, str] | None = None,
    _fact_unit: FactUnit | None = None,
    _source_contract: SourceContract | None = None,
) -> AdmitResult:
    """Idempotent by Item identity. Returns the Event assignment for the (possibly pre-existing) Item.

    A member that joins an existing non-candidate Event re-runs the Gate when it is stronger evidence (score >= 80,
    an A/A+ grounded tag, or a different reporting origin); the Event is then upgraded in place and published once.
    """

    news = repos.news
    metadata = dict(event.provider_metadata)
    source_contract = _source_contract or classify_source_contract(metadata)
    source_contract_reason: SourceContractReason | None = source_contract.reason
    contract_engine = source_contract.identity.engine_type
    engine_type = contract_engine if contract_engine in {"news", "meme", "listing", "market"} else "unknown"
    strategy_ids = tuple(
        str(s.get("id")) for s in (metadata.get("strategies") or []) if isinstance(s, Mapping) and s.get("id")
    )
    raw_text = text_override if text_override is not None else (event.raw_text or _reconstruct_text(event))
    parent_extracted = extract_title(raw_text)
    item_id = item_identity(source_id=OPENNEWS_SOURCE_ID, source_item_key=event.provider_record_id)
    fact = (
        _fact_unit
        or extract_fact_units(
            item_id=item_id,
            raw_text=raw_text,
            fallback_title=parent_extracted.title or (event.entry.title or "")[:500],
        )[0]
    )
    extracted = extract_title(fact.text)
    title = extracted.title or fact.text[:500]
    comparison = extracted.comparison
    family_name = dedupe_family(comparison)
    fingerprint = hashlib.sha256(comparison.encode("utf-8")).hexdigest()
    published_at_ms = int(event.entry.published_at_ms or observed_at_ms)
    inserted = news.upsert_item(
        item_id=item_id,
        source_id=OPENNEWS_SOURCE_ID,
        source_item_key=event.provider_record_id,
        title=parent_extracted.title or title or "(untitled)",
        raw_first_line=parent_extracted.first_line[:500],
        description=description_after_title(raw_text) or event.entry.description or "",
        canonical_url=event.entry.link,
        reporting_origin=event.entry.reporting_origin or "opennews",
        published_at_ms=published_at_ms,
        observed_at_ms=int(observed_at_ms),
        provider_metadata=metadata,
        strategy_ids=strategy_ids,
        ingest_mode=ingest_mode,
        trace_id=trace_id,
        now_ms=now_ms,
        source_artifact_id=event.source_artifact_id,
    )
    if source_contract.source_contract_family == "liquidation_v1":
        liquidation = liquidations.parse_liquidation(
            title,
            item_id=item_id,
            fact_id=fact.fact_id,
            provider_source=str(metadata.get("source") or ""),
            event_at_ms=published_at_ms,
            received_at_ms=int(observed_at_ms),
            provider_record_identity=event.provider_record_id,
        )
        if liquidation is None:
            source_contract_reason = "source_contract_drift"
        else:
            news.insert_market_liquidation(
                source_key=liquidation.source_key,
                item_id=liquidation.item_id,
                fact_id=liquidation.fact_id,
                ingest_mode=ingest_mode,
                symbol=liquidation.symbol,
                venue=liquidation.venue,
                liquidated_position_side=liquidation.liquidated_position_side,
                forced_order_side=liquidation.forced_order_side,
                notional_usd=liquidation.notional_usd,
                quantity=liquidation.quantity,
                price=liquidation.price,
                event_at_ms=liquidation.event_at_ms,
                received_at_ms=liquidation.received_at_ms,
                parser_version=liquidation.parser_version,
                provider_record_identity=liquidation.provider_record_identity,
                symbol_contract_identity=liquidation.symbol_contract_identity,
                position_side_semantics=liquidation.position_side_semantics,
                quantity_semantics=liquidation.quantity_semantics,
                notional_semantics=liquidation.notional_semantics,
                price_semantics=liquidation.price_semantics,
                completeness_assumption=liquidation.completeness_assumption,
                throttle_assumption=liquidation.throttle_assumption,
                source_contract_version=liquidation.source_contract_version,
                source_contract_complete=liquidation.source_contract_complete,
                now_ms=now_ms,
            )
    # Admission exercises the strict parser in both live and recovery modes so contract drift is durable
    # even when recovery intentionally never reaches Triage. This parse is side-effect free: OI's ranked
    # fact remains live-only and is still written by the deterministic judge.
    if source_contract.source_contract_family == "oi_v1" and oi_signals.parse_oi_signal(title) is None:
        source_contract_reason = "source_contract_drift"
    existing_membership = news.fact_membership(
        item_id=item_id,
        fact_id=fact.fact_id,
        event_kind=source_contract.event_kind,
    )
    if existing_membership is not None:
        ev = news.event_admission(str(existing_membership["event_id"]))
        return AdmitResult(
            item_id=item_id,
            item_inserted=inserted,
            event_id=str(existing_membership["event_id"]),
            event_created=False,
            admission=str(ev["admission"]) if ev else "candidate",
            event_kind=str(ev["event_kind"]) if ev else source_contract.event_kind,  # type: ignore[arg-type]
            match_kind=str(existing_membership["match_kind"]),
            gate=None,
            dedupe_family=family_name,
            storyline_key=str(ev["storyline_key"]) if ev else "",
            comparison_fingerprint=fingerprint,
            title=title,
        )

    coins = tuple(c for c in (metadata.get("coins") or []) if isinstance(c, Mapping))
    provider_score = metadata.get("score")
    # A split unit's shared evidence is the digest's lead, not the parent's first line — which on a bare numbered
    # digest *is* bullet 1.  The Gate reads `title + raw_first_line` for cashtags and for the macro/energy/PR
    # lexicons, so the old value grounded every bullet on whatever the first one happened to say.  This is the same
    # text the model receives as `content`, so the two now agree on what the digest's shared context is.
    gate_context = fact.context if fact.method == "explicit_numbered" else parent_extracted.first_line
    gate = evaluate_gate(
        GateInput(
            title=title,
            engine_type=engine_type,  # type: ignore[arg-type]
            provider_score=float(provider_score) if isinstance(provider_score, (int, float)) else None,
            coins=coins,
            ingest_mode=ingest_mode,
            watchlist_symbols=watchlist_symbols,
            raw_first_line=gate_context,
            suppress_low_signal=suppress_low_signal,
            instrument_classes=instrument_classes,
        )
    )
    gate = replace(
        gate,
        admission=source_contract_admission(
            source_contract,
            generic_admission=gate.admission,
            ingest_mode=ingest_mode,
        ),
    )
    tokens = comparison_tokens(comparison)
    window_ms = dedupe_window_ms(family_name)
    shareable = len(tokens) >= 3

    exact = (
        news.find_exact_event(
            dedupe_family=family_name,
            event_kind=source_contract.event_kind,
            fingerprint=fingerprint,
            now_ms=now_ms,
            source_contract_reason=source_contract_reason,
        )
        if shareable
        else None
    )
    if exact is not None and int(exact["opened_at_ms"]) + window_ms <= published_at_ms:
        exact = None
    if exact is None:
        # #154: the same source artifact, the same fact, a longer horizon. The text-derived path above needs
        # its three-token floor and its 12 h window because text similarity is only evidence; a status id is
        # the platform's own primary key, so neither guard applies to it.
        exact = news.find_artifact_event(
            source_artifact_id=event.source_artifact_id,
            dedupe_family=family_name,
            event_kind=source_contract.event_kind,
            fingerprint=fingerprint,
            item_id=item_id,
            opened_after_ms=published_at_ms - ARTIFACT_WINDOW_MS,
            source_contract_reason=source_contract_reason,
        )
    if exact is not None:
        news.add_member(
            event_id=str(exact["event_id"]),
            item_id=item_id,
            joined_at_ms=published_at_ms,
            match_kind="exact",
            jaccard_estimate=1.0,
            provider_score=float(provider_score) if isinstance(provider_score, (int, float)) else None,
            fact_id=fact.fact_id,
            fact_text=fact.text,
            now_ms=now_ms,
        )
        result = _member_result(
            repos,
            event_id=str(exact["event_id"]),
            item_id=item_id,
            inserted=inserted,
            match_kind="exact",
            gate=gate,
            event_kind=source_contract.event_kind,
            dedupe_family=family_name,
            fingerprint=fingerprint,
            title=title,
            reporting_origin=event.entry.reporting_origin or "opennews",
            now_ms=now_ms,
        )
        news.append_evidence_snapshot(
            event_id=result.event_id,
            now_ms=now_ms,
            focus_item_id=item_id if result.evidence_focus_changed else None,
            focus_fact=fact if result.evidence_focus_changed else None,
        )
        return result

    if shareable:
        signature = minhash_signature(tokens)
        keys = band_keys(signature)
        mine = _strong_facts(title, gate.grounded_assets)
        best_id, best_j = None, 0.0
        # Telemetry frames are exempt from near-duplicate matching (#137). Two frames for one symbol
        # differ only in their four numbers, which is their entire content: they score 0.60 against
        # each other, so the second observation would join the first as a member, never reach Triage,
        # and never count toward its own rank. Byte-identical redeliveries are still collapsed by the
        # exact fingerprint above.
        #
        # Keyed on source-contract kind and result rather than live/recovery admission: a format-drift
        # or recovered deterministic frame must remain its own measurement.
        candidates = (
            ()
            if source_contract.event_kind in {"oi", "liquidation"}
            else news.find_band_candidates(
                dedupe_family=family_name,
                event_kind=source_contract.event_kind,
                band_keys=keys,
                now_ms=now_ms,
                source_contract_reason=source_contract_reason,
            )
        )
        for cand in candidates:
            cand_tokens = comparison_tokens(str(cand["comparison_title"]))
            j = jaccard(tokens, cand_tokens)
            if j >= NEAR_DUPLICATE_THRESHOLD and j > best_j:
                theirs = _strong_facts(str(cand["leader_title"]), list(cand.get("grounded_assets") or []))
                if _compatible(mine, theirs):
                    best_id, best_j = str(cand["event_id"]), j
        if best_id is not None:
            news.add_member(
                event_id=best_id,
                item_id=item_id,
                joined_at_ms=published_at_ms,
                match_kind="near",
                jaccard_estimate=round(best_j, 4),
                provider_score=float(provider_score) if isinstance(provider_score, (int, float)) else None,
                fact_id=fact.fact_id,
                fact_text=fact.text,
                now_ms=now_ms,
            )
            result = _member_result(
                repos,
                event_id=best_id,
                item_id=item_id,
                inserted=inserted,
                match_kind="near",
                gate=gate,
                event_kind=source_contract.event_kind,
                dedupe_family=family_name,
                fingerprint=fingerprint,
                title=title,
                reporting_origin=event.entry.reporting_origin or "opennews",
                now_ms=now_ms,
            )
            news.append_evidence_snapshot(
                event_id=result.event_id,
                now_ms=now_ms,
                focus_item_id=item_id if result.evidence_focus_changed else None,
                focus_fact=fact if result.evidence_focus_changed else None,
            )
            return result
    else:
        keys = ()

    storyline = preliminary_storyline_key(
        title=title,
        grounded_assets=gate.strong_assets,
        asset_class=gate.asset_class,
        dedupe_family=family_name,
    )
    context_line = f"[{gate.asset_class}/{family_name}/{engine_type}] " + " ".join(gate.grounded_assets)
    event_id = _event_identity(
        item_id=item_id,
        fact=fact,
        event_kind=source_contract.event_kind,
    )
    news.insert_event(
        event_id=event_id,
        leader_item_id=item_id,
        dedupe_family=family_name,
        event_kind=source_contract.event_kind,
        comparison_fingerprint=fingerprint,
        comparison_title=comparison,
        leader_title=title or "(untitled)",
        focus_fact_id=fact.fact_id,
        focus_fact_text=fact.text,
        focus_fact_context=fact.context,
        focus_fact_method=fact.method,
        focus_span_start=fact.span_start,
        focus_span_end=fact.span_end,
        opened_at_ms=published_at_ms,
        expires_at_ms=published_at_ms + window_ms,
        admission=gate.admission,
        queue_priority=gate.queue_priority,
        provider_score=float(provider_score) if isinstance(provider_score, (int, float)) else None,
        engine_type=engine_type,
        asset_class=gate.asset_class,
        grounded_assets=gate.grounded_assets,
        watchlist_hits=gate.watchlist_hits,
        macro_lexicon=gate.macro_lexicon,
        storyline_key=storyline,
        context_line=context_line.strip(),
        ingest_mode=ingest_mode,
        trace_id=trace_id,
        band_keys=keys if shareable else (),
        now_ms=now_ms,
        source_contract_reason=source_contract_reason,
    )
    news.append_evidence_snapshot(event_id=event_id, now_ms=now_ms)
    return AdmitResult(
        item_id=item_id,
        item_inserted=inserted,
        event_id=event_id,
        event_created=True,
        admission=gate.admission,
        event_kind=source_contract.event_kind,
        match_kind="leader",
        gate=gate,
        dedupe_family=family_name,
        storyline_key=storyline,
        comparison_fingerprint=fingerprint,
        title=title,
    )


def _member_result(
    repos: Any,
    *,
    event_id: str,
    item_id: str,
    inserted: bool,
    match_kind: str,
    gate: GateVerdict,
    event_kind: EventKind,
    dedupe_family: str,
    fingerprint: str,
    title: str,
    reporting_origin: str,
    now_ms: int,
) -> AdmitResult:
    """Attach a member and, when the member is stronger evidence than the leader, re-gate a suppressed Event."""

    row = repos.news.event_regate_context(event_id)
    admission = str(row["admission"]) if row else "candidate"
    upgraded = False
    stronger = False
    if row:
        leader_metadata = dict(row["leader_provider_metadata"] or {})
        try:
            leader_score = float(leader_metadata.get("score") or 0)
        except (TypeError, ValueError):
            leader_score = 0.0
        member_score = _member_score(repos, item_id)
        stronger = (
            member_score > leader_score
            or (bool(gate.grounded_assets) and _strong_tag(gate))
            or member_score >= _STRONG_MEMBER_SCORE
            or (bool(reporting_origin) and reporting_origin != str(row["leader_origin"] or ""))
        )
    if row and admission not in _REGATE_ADMISSIONS and gate.admission == "candidate" and stronger:
        repos.news.upgrade_event_admission(
            event_id=event_id,
            admission="candidate",
            queue_priority=gate.queue_priority,
            asset_class=gate.asset_class,
            grounded_assets=gate.grounded_assets,
            watchlist_hits=gate.watchlist_hits,
            macro_lexicon=gate.macro_lexicon,
            now_ms=now_ms,
        )
        admission, upgraded = "candidate", True
    return AdmitResult(
        item_id=item_id,
        item_inserted=inserted,
        event_id=event_id,
        event_created=upgraded,  # an upgraded Event is published exactly like a new candidate
        admission=admission,
        event_kind=event_kind,
        match_kind=match_kind,
        gate=gate,
        dedupe_family=dedupe_family,
        storyline_key=str(row["storyline_key"]) if row else "",
        comparison_fingerprint=fingerprint,
        title=title,
        evidence_focus_changed=stronger,
    )


def _strong_tag(gate: GateVerdict) -> bool:
    """A later member that adds objective grounding to a previously suppressed Event.

    Queue priority is deliberately absent: it schedules broker work and has no editorial authority.
    """

    return bool(gate.grounded_assets) or bool(gate.watchlist_hits)


def _member_score(repos: Any, item_id: str) -> float:
    row = repos.news.item_provider_score(item_id)
    try:
        return float(row["score"]) if row and row["score"] is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _reconstruct_text(event: OpenNewsEvent) -> str:
    """The adapter keeps title/description; rebuild a text blob so extract_title sees the same blocks."""

    parts = [event.entry.title or ""]
    if event.entry.description:
        parts.append(event.entry.description)
    return "<br/>".join(p for p in parts if p)


async def publish_event(
    bus: Any, db: NewsDatabasePort, *, event_id: str, dedupe_family: str, queue_priority: str, trace_id: str
) -> None:
    """Publish one candidate Event to Triage and mark it published (commit-then-publish outbox step)."""

    stamp = now_ms()
    await bus.publish(
        BusMessage(
            kind="event",
            message_id=f"event:{event_id}",
            routing_key=RK_EVENT.format(dedupe_family=dedupe_family, queue_priority=queue_priority),
            payload={"event_id": event_id},
            trace_id=trace_id,
            occurred_at_ms=stamp,
            priority=5 if queue_priority == "high" else 0,
        )
    )
    with contextlib.suppress(TransientError, DeferError):
        await db.tx(
            "news_event_mark_published",
            lambda repos: repos.news.mark_event_published(event_id=event_id, now_ms=stamp),
            timeout_seconds=1.0,
        )


class DeduperConsumer:
    work_semantics: ClassVar[tuple[NewsWorkSemantics, ...]] = ("durable_event",)

    def __init__(
        self,
        *,
        bus: Any,
        db: NewsDatabasePort,
        watchlist_symbols: frozenset[str],
        suppress_low_signal: bool = False,
    ) -> None:
        self.bus = bus
        self.db = db
        self.watchlist_symbols = watchlist_symbols
        self.suppress_low_signal = bool(suppress_low_signal)
        # #89: symbol -> instrument_class, which is how the Gate tells a stock headline from a coin headline. The
        # universe changes about once a day, so it is cached per consumer.
        self._classes: Mapping[str, str] | None = None
        self._classes_at_ms = 0

    def _current_instrument_classes(self, repos: Any, *, now: int) -> Mapping[str, str] | None:
        """Cached instrument classes for the Gate, refreshed at most once per `_INSTRUMENT_CACHE_TTL_MS`."""

        if self._classes is not None and now - self._classes_at_ms < _INSTRUMENT_CACHE_TTL_MS:
            return self._classes
        classes = repos.instruments.instrument_classes()
        self._classes_at_ms = now
        # An empty universe means no snapshot has landed: fall back to the prefix heuristic, not to "no assets".
        self._classes = classes or None
        return self._classes

    async def run(self, *, stop_event: asyncio.Event) -> None:
        await self.bus.consume(Q_RAW, self.handle, prefetch=1, stop_event=stop_event)

    async def handle(self, message: BusMessage) -> None:
        params = message.payload.get("params")
        if not isinstance(params, Mapping):
            raise PermanentError("news_raw_params_missing")
        event = parse_opennews_message({"method": "strategy.triggered", "params": dict(params)})
        if event is None:
            return  # malformed frame: settle silently
        ingest_mode = "recovery" if str(message.payload.get("ingest_mode")) == "recovery" else "live"
        observed = int(message.payload.get("observed_at_ms") or message.occurred_at_ms or now_ms())
        stamp = now_ms()
        batch = await self.db.tx(
            "news_deduper_admit",
            lambda repos: admit_frame(
                repos,
                event=event,
                ingest_mode=ingest_mode,
                observed_at_ms=observed,
                trace_id=message.trace_id,
                watchlist_symbols=self.watchlist_symbols,
                now_ms=stamp,
                suppress_low_signal=self.suppress_low_signal,
                instrument_classes=self._current_instrument_classes(repos, now=stamp),
            ),
            timeout_seconds=5.0,
        )
        for result in batch.results:
            if result.event_created and result.admission in ADMITTED_ADMISSIONS:
                await publish_event(
                    self.bus,
                    self.db,
                    event_id=result.event_id,
                    dedupe_family=result.dedupe_family,
                    queue_priority=result.gate.queue_priority if result.gate else "normal",
                    trace_id=message.trace_id,
                )


__all__ = [
    "EVENT_IDENTITY_VERSION",
    "NEAR_DUPLICATE_THRESHOLD",
    "AdmitBatchResult",
    "AdmitResult",
    "DeduperConsumer",
    "admit_frame",
    "admit_item",
    "item_identity",
    "publish_event",
]
