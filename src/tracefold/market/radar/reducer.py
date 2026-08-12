from __future__ import annotations

import hashlib
import heapq
import json
import math
import re
import time
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from tracefold.market.pricing.live_market import LIVE_MARKET_STALE_AFTER_MS
from tracefold.market.radar.constants import (
    TOKEN_RADAR_CURRENT_WINDOW_MS,
    TOKEN_RADAR_EPISODE_TTL_MS,
    TOKEN_RADAR_INPUT_BYTE_CAP,
    TOKEN_RADAR_INPUT_ROW_CAP,
    TOKEN_RADAR_LIVE_LAG_MS,
    TOKEN_RADAR_MAX_ITEMS,
    TOKEN_RADAR_OUTPUT_BYTE_CAP,
    TOKEN_RADAR_PRIOR_WINDOW_MS,
    TOKEN_RADAR_REPLAY_TRANSITION_MS,
    TOKEN_RADAR_SEMANTICS,
    TOKEN_RADAR_SEMANTICS_FINGERPRINT,
    TOKEN_RADAR_SNAPSHOT_SCHEMA_VERSION,
    TOKEN_RADAR_SOURCE_HORIZON_MS,
)

_SPACE_RE = re.compile(r"\s+")
_FINGERPRINT_SPACE_RE = re.compile(r"[ \t\n\r\f]+")
_ASCII_LOWER_TRANSLATION = str.maketrans(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
    "abcdefghijklmnopqrstuvwxyz",
)
_LOCAL_TOKEN_IMAGE_RE = re.compile(r"^/api/token-images/[0-9a-f]{64}$")
_TEXT_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{32}$")
_MISSING_TEXT_KEY = "<missing>"

TargetKey = tuple[str, str]
BindingKey = tuple[str, TargetKey]


class TokenRadarInputOverflow(RuntimeError):
    """The material-fact input exceeded a hard reducer envelope."""


class TokenRadarBudgetExceeded(RuntimeError):
    """The deterministic reducer exceeded its code-owned CPU deadline."""


class TokenRadarOutputOverflow(RuntimeError):
    """The compact public packet exceeded its uncompressed byte envelope."""


class TokenRadarInvariantViolation(RuntimeError):
    """Persisted facts cannot produce one coherent public Radar packet."""


@dataclass(frozen=True, slots=True)
class RadarEvidenceRevision:
    event_id: str
    intent_id: str
    resolution_id: str
    source_event_at_ms: int
    received_at_ms: int
    event_created_at_ms: int
    action: str
    author_key: str | None
    text_fingerprint: str | None
    resolution_status: str
    target_type: str | None
    target_id: str | None
    resolution_decision_at_ms: int
    resolution_created_at_ms: int

    def __post_init__(self) -> None:
        required_text = {
            "event_id": "token_radar_event_id_required",
            "intent_id": "token_radar_intent_id_required",
            "resolution_id": "token_radar_resolution_id_required",
            "action": "token_radar_action_required",
            "resolution_status": "token_radar_resolution_status_required",
        }
        for field_name, error_code in required_text.items():
            object.__setattr__(self, field_name, _required_text(getattr(self, field_name), error_code))
        required_clocks = {
            "source_event_at_ms": "token_radar_source_time_invalid",
            "received_at_ms": "token_radar_received_time_invalid",
            "event_created_at_ms": "token_radar_event_created_time_invalid",
            "resolution_decision_at_ms": "token_radar_resolution_decision_time_invalid",
            "resolution_created_at_ms": "token_radar_resolution_created_time_invalid",
        }
        for field_name, error_code in required_clocks.items():
            object.__setattr__(self, field_name, _nonnegative_int(getattr(self, field_name), error_code))
        object.__setattr__(self, "author_key", _normalized_author(self.author_key))
        fingerprint = _text(self.text_fingerprint)
        if fingerprint is not None and _TEXT_FINGERPRINT_RE.fullmatch(fingerprint) is None:
            raise TokenRadarInvariantViolation("token_radar_text_fingerprint_invalid")
        object.__setattr__(self, "text_fingerprint", fingerprint)
        object.__setattr__(self, "target_type", _text(self.target_type))
        object.__setattr__(self, "target_id", _text(self.target_id))


@dataclass(frozen=True, slots=True)
class RadarSelectionKey:
    target_type: str
    target_id: str
    trigger_event_id: str
    trigger_intent_id: str
    trigger_resolution_id: str


@dataclass(frozen=True, slots=True)
class ReducedTokenRadar:
    ruleset_version: str
    ruleset_fingerprint: str
    input_fingerprint: str
    state_fingerprint: str
    input_rows: int
    input_bytes: int
    output_bytes: int
    eligible_rows: int
    snapshot: dict[str, Any]
    selected_keys: tuple[RadarSelectionKey, ...]


@dataclass(frozen=True, slots=True)
class _EvidenceEvent:
    event_id: str
    source_event_at_ms: int
    author_key: str | None
    text_key: str


@dataclass(frozen=True, slots=True)
class _BindingChange:
    effective_at_ms: int
    delta: Literal[-1, 1]
    event: _EvidenceEvent
    target: TargetKey
    intent_id: str
    resolution_id: str

    @property
    def binding_key(self) -> BindingKey:
        return (self.event.event_id, self.target)


@dataclass(frozen=True, slots=True)
class _Episode:
    qualified_at_ms: int
    trigger: _BindingChange


@dataclass(slots=True)
class _TargetState:
    current: dict[str, _EvidenceEvent] = field(default_factory=dict)
    prior: dict[str, _EvidenceEvent] = field(default_factory=dict)
    author_events: dict[str, dict[str, int]] = field(default_factory=dict)
    author_heaps: dict[str, list[tuple[int, str]]] = field(default_factory=dict)
    author_versions: dict[str, int] = field(default_factory=dict)
    author_heads: list[tuple[int, str, str, int]] = field(default_factory=list)
    text_counts: Counter[str] = field(default_factory=Counter)
    duplicate_events: int = 0
    gate_is_true: bool = False
    episode: _Episode | None = None

    def add_evidence(self, event: _EvidenceEvent, *, at_ms: int) -> bool:
        if event.event_id in self.current or event.event_id in self.prior:
            return False
        current_start = max(0, at_ms - TOKEN_RADAR_CURRENT_WINDOW_MS)
        prior_start = max(0, current_start - TOKEN_RADAR_PRIOR_WINDOW_MS)
        if current_start <= event.source_event_at_ms <= at_ms:
            self._add_current(event)
            return True
        if prior_start < event.source_event_at_ms < current_start:
            self.prior[event.event_id] = event
            return True
        return False

    def remove_evidence(self, event_id: str) -> bool:
        if event_id in self.current:
            self._remove_current(event_id)
            return True
        return self.prior.pop(event_id, None) is not None

    def move_current_to_prior(self, event_id: str) -> bool:
        event = self._remove_current(event_id)
        if event is None:
            return False
        self.prior[event_id] = event
        return True

    def remove_prior(self, event_id: str) -> bool:
        return self.prior.pop(event_id, None) is not None

    def expire_episode(self, *, at_ms: int) -> None:
        if self.episode is not None and self.episode.qualified_at_ms + TOKEN_RADAR_EPISODE_TTL_MS <= at_ms:
            self.episode = None

    def gate(self) -> bool:
        propagation_ms = self.time_to_nth_author_ms()
        return (
            len(self.current) - len(self.prior) >= TOKEN_RADAR_SEMANTICS.minimum_attention_delta
            and len(self.author_events) >= TOKEN_RADAR_SEMANTICS.minimum_independent_authors
            and self.duplicate_share() <= TOKEN_RADAR_SEMANTICS.maximum_duplicate_share
            and propagation_ms is not None
            and propagation_ms <= TOKEN_RADAR_SEMANTICS.maximum_propagation_ms
        )

    def duplicate_share(self) -> float:
        return self.duplicate_events / len(self.current) if self.current else 0.0

    def time_to_nth_author_ms(self) -> int | None:
        required = TOKEN_RADAR_SEMANTICS.minimum_independent_authors
        heads: list[tuple[int, str, str, int]] = []
        while self.author_heads and len(heads) < required:
            candidate = heapq.heappop(self.author_heads)
            if self._valid_author_head(candidate):
                heads.append(candidate)
        for candidate in heads:
            heapq.heappush(self.author_heads, candidate)
        if len(heads) < required:
            return None
        return max(0, heads[-1][0] - heads[0][0])

    def independent_text_count(self) -> int:
        return len(self.text_counts) - int(_MISSING_TEXT_KEY in self.text_counts)

    def _add_current(self, event: _EvidenceEvent) -> None:
        self.current[event.event_id] = event
        previous_text_count = self.text_counts[event.text_key]
        if previous_text_count > 0:
            self.duplicate_events += 1
        self.text_counts[event.text_key] = previous_text_count + 1
        if event.author_key is None:
            return
        author_events = self.author_events.setdefault(event.author_key, {})
        author_events[event.event_id] = event.source_event_at_ms
        author_heap = self.author_heaps.setdefault(event.author_key, [])
        heapq.heappush(author_heap, (event.source_event_at_ms, event.event_id))
        self._refresh_author_head(event.author_key)

    def _remove_current(self, event_id: str) -> _EvidenceEvent | None:
        event = self.current.pop(event_id, None)
        if event is None:
            return None
        previous_text_count = self.text_counts[event.text_key]
        if previous_text_count > 1:
            self.duplicate_events -= 1
            self.text_counts[event.text_key] = previous_text_count - 1
        else:
            self.text_counts.pop(event.text_key, None)
        if event.author_key is not None:
            author_events = self.author_events[event.author_key]
            author_events.pop(event.event_id, None)
            if not author_events:
                self.author_events.pop(event.author_key, None)
            self._refresh_author_head(event.author_key)
        return event

    def _refresh_author_head(self, author: str) -> None:
        version = self.author_versions.get(author, 0) + 1
        self.author_versions[author] = version
        events = self.author_events.get(author, {})
        author_heap = self.author_heaps.setdefault(author, [])
        while author_heap and author_heap[0][1] not in events:
            heapq.heappop(author_heap)
        if not events:
            self.author_heaps.pop(author, None)
            return
        source_at_ms, event_id = author_heap[0]
        heapq.heappush(self.author_heads, (source_at_ms, author, event_id, version))

    def _valid_author_head(self, candidate: tuple[int, str, str, int]) -> bool:
        source_at_ms, author, event_id, version = candidate
        if self.author_versions.get(author) != version:
            return False
        events = self.author_events.get(author)
        if not events or events.get(event_id) != source_at_ms:
            return False
        author_heap = self.author_heaps.get(author)
        return bool(author_heap and author_heap[0] == (source_at_ms, event_id))


def reduce_token_radar(
    revisions: Sequence[RadarEvidenceRevision],
    *,
    now_ms: int,
    deadline_monotonic: float | None = None,
) -> ReducedTokenRadar:
    """Replay bounded resolution-aware facts into causal four-hour Radar episodes."""

    parsed_now_ms = _nonnegative_int(now_ms, "token_radar_now_ms_invalid")
    input_rows = len(revisions)
    if input_rows > TOKEN_RADAR_INPUT_ROW_CAP:
        raise TokenRadarInputOverflow("token_radar_input_row_overflow")
    input_bytes = token_radar_input_size(revisions)
    if input_bytes > TOKEN_RADAR_INPUT_BYTE_CAP:
        raise TokenRadarInputOverflow("token_radar_input_byte_overflow")
    _check_deadline(deadline_monotonic)

    canonical_inputs = [asdict(revision) for revision in sorted(revisions, key=_canonical_revision_sort_key)]
    input_fingerprint = _fingerprint(canonical_inputs)
    replay_start_ms = max(0, parsed_now_ms - TOKEN_RADAR_REPLAY_TRANSITION_MS)
    source_horizon_ms = max(0, parsed_now_ms - TOKEN_RADAR_SOURCE_HORIZON_MS)
    live_revisions = [
        revision
        for revision in revisions
        if _event_is_live_eligible(revision, now_ms=parsed_now_ms, source_horizon_ms=source_horizon_ms)
        and revision.resolution_created_at_ms <= parsed_now_ms
    ]
    social_evidence_as_of_ms = max(
        (max(revision.event_created_at_ms, revision.resolution_created_at_ms) for revision in live_revisions),
        default=0,
    )
    events = _events(live_revisions)
    changes = _binding_changes(live_revisions)
    states = _replay(
        events,
        changes,
        replay_start_ms=replay_start_ms,
        now_ms=parsed_now_ms,
        deadline_monotonic=deadline_monotonic,
    )

    candidates: list[dict[str, Any]] = []
    for index, (target, state) in enumerate(sorted(states.items())):
        if index % 64 == 0:
            _check_deadline(deadline_monotonic)
        state.expire_episode(at_ms=parsed_now_ms)
        if state.episode is None or not state.gate_is_true:
            continue
        episode = state.episode
        trigger = episode.trigger
        propagation_ms = state.time_to_nth_author_ms()
        if propagation_ms is None:
            raise TokenRadarInvariantViolation("token_radar_propagation_missing")
        item = {
            "target": {"target_type": target[0], "target_id": target[1]},
            "trigger_event_id": trigger.event.event_id,
            "trigger_source_event_at_ms": trigger.event.source_event_at_ms,
            "qualified_at_ms": episode.qualified_at_ms,
            "why_now": {
                "current_mentions": len(state.current),
                "prior_mentions": len(state.prior),
                "mention_delta": len(state.current) - len(state.prior),
            },
            "evidence": {
                "independent_author_count": len(state.author_events),
                "independent_text_count": state.independent_text_count(),
                "time_to_nth_author_ms": propagation_ms,
                "duplicate_share": state.duplicate_share(),
            },
            "market": _unavailable_market(),
        }
        candidates.append(
            {
                "item": item,
                "selection_key": RadarSelectionKey(
                    target_type=target[0],
                    target_id=target[1],
                    trigger_event_id=trigger.event.event_id,
                    trigger_intent_id=trigger.intent_id,
                    trigger_resolution_id=trigger.resolution_id,
                ),
                "rank": (-episode.qualified_at_ms, target[0], target[1]),
            }
        )

    candidates.sort(key=lambda candidate: tuple(candidate["rank"]))
    eligible_rows = len(candidates)
    selected = candidates[:TOKEN_RADAR_MAX_ITEMS]
    snapshot = {
        "schema_version": TOKEN_RADAR_SNAPSHOT_SCHEMA_VERSION,
        "social_evidence_as_of_ms": social_evidence_as_of_ms,
        "eligible_total": eligible_rows,
        "items": [candidate["item"] for candidate in selected],
    }
    return _reduced(
        snapshot=snapshot,
        input_fingerprint=input_fingerprint,
        input_rows=input_rows,
        input_bytes=input_bytes,
        eligible_rows=eligible_rows,
        selected_keys=tuple(candidate["selection_key"] for candidate in selected),
    )


def enrich_token_radar(
    reduced: ReducedTokenRadar,
    rows: Sequence[Mapping[str, Any]],
    *,
    now_ms: int,
) -> ReducedTokenRadar:
    """Attach bounded identity and market facts after causal Top-50 selection."""

    parsed_now_ms = _nonnegative_int(now_ms, "token_radar_now_ms_invalid")
    facts_by_target = {
        (str(fact["target_type"]), str(fact["target_id"])): fact
        for fact in (_canonical_presentation_fact(row) for row in rows)
    }
    enriched_items: list[dict[str, Any]] = []
    for source_item in reduced.snapshot["items"]:
        source_target = source_item["target"]
        target = (str(source_target["target_type"]), str(source_target["target_id"]))
        fact = facts_by_target.get(target)
        if fact is None:
            raise TokenRadarInvariantViolation("token_radar_presentation_fact_missing")
        identity = _public_identity(target, fact)
        enriched_items.append(
            {
                **source_item,
                "target": identity,
                "market": _presentation_market(
                    fact,
                    signal_price_usd=fact.get("signal_price_usd"),
                    trigger_source_event_at_ms=int(source_item["trigger_source_event_at_ms"]),
                    now_ms=parsed_now_ms,
                ),
            }
        )

    snapshot = {
        "schema_version": TOKEN_RADAR_SNAPSHOT_SCHEMA_VERSION,
        "social_evidence_as_of_ms": int(reduced.snapshot["social_evidence_as_of_ms"]),
        "eligible_total": int(reduced.snapshot["eligible_total"]),
        "items": enriched_items,
    }
    return _reduced(
        snapshot=snapshot,
        input_fingerprint=reduced.input_fingerprint,
        input_rows=reduced.input_rows,
        input_bytes=reduced.input_bytes,
        eligible_rows=reduced.eligible_rows,
        selected_keys=reduced.selected_keys,
    )


def token_radar_input_size(revisions: Sequence[RadarEvidenceRevision]) -> int:
    return 2 + sum(
        len(_canonical_json_bytes(asdict(revision))) + int(index > 0) for index, revision in enumerate(revisions)
    )


def token_radar_input_row_size(revision: RadarEvidenceRevision) -> int:
    return len(_canonical_json_bytes(asdict(revision)))


def _reduced(
    *,
    snapshot: dict[str, Any],
    input_fingerprint: str,
    input_rows: int,
    input_bytes: int,
    eligible_rows: int,
    selected_keys: tuple[RadarSelectionKey, ...],
) -> ReducedTokenRadar:
    output_bytes = len(
        _canonical_json_bytes(
            {
                "schema_version": TOKEN_RADAR_SNAPSHOT_SCHEMA_VERSION,
                "state": "stale",
                "stale_reason": "projection_failed",
                "state_changed_at_ms": 9_223_372_036_854_775_807,
                "social_evidence_as_of_ms": snapshot["social_evidence_as_of_ms"],
                "eligible_total": snapshot["eligible_total"],
                "items": snapshot["items"],
            }
        )
    )
    if output_bytes > TOKEN_RADAR_OUTPUT_BYTE_CAP:
        raise TokenRadarOutputOverflow("token_radar_output_byte_overflow")
    return ReducedTokenRadar(
        ruleset_version=TOKEN_RADAR_SEMANTICS.version,
        ruleset_fingerprint=TOKEN_RADAR_SEMANTICS_FINGERPRINT,
        input_fingerprint=input_fingerprint,
        state_fingerprint=_fingerprint(snapshot),
        input_rows=input_rows,
        input_bytes=input_bytes,
        output_bytes=output_bytes,
        eligible_rows=eligible_rows,
        snapshot=snapshot,
        selected_keys=selected_keys,
    )


def _event_is_live_eligible(
    revision: RadarEvidenceRevision,
    *,
    now_ms: int,
    source_horizon_ms: int,
) -> bool:
    event_lag = revision.received_at_ms - revision.source_event_at_ms
    return (
        revision.action in TOKEN_RADAR_SEMANTICS.actions
        and source_horizon_ms < revision.source_event_at_ms <= now_ms
        and 0 <= event_lag <= TOKEN_RADAR_LIVE_LAG_MS
        and revision.event_created_at_ms <= now_ms
    )


def _resolution_is_timely(revision: RadarEvidenceRevision) -> bool:
    lag = revision.resolution_created_at_ms - revision.received_at_ms
    return 0 <= lag <= TOKEN_RADAR_LIVE_LAG_MS


def _resolved_target(revision: RadarEvidenceRevision) -> TargetKey | None:
    if (
        revision.resolution_status not in TOKEN_RADAR_SEMANTICS.resolution_statuses
        or revision.target_type not in TOKEN_RADAR_SEMANTICS.target_types
        or revision.target_id is None
    ):
        return None
    return (revision.target_type, revision.target_id)


def _events(revisions: Sequence[RadarEvidenceRevision]) -> dict[str, _EvidenceEvent]:
    events: dict[str, _EvidenceEvent] = {}
    for revision in revisions:
        event = _EvidenceEvent(
            event_id=revision.event_id,
            source_event_at_ms=revision.source_event_at_ms,
            author_key=revision.author_key,
            text_key=revision.text_fingerprint or _MISSING_TEXT_KEY,
        )
        existing = events.setdefault(revision.event_id, event)
        if existing != event:
            raise TokenRadarInvariantViolation("token_radar_event_revision_mismatch")
    return events


def _binding_changes(revisions: Sequence[RadarEvidenceRevision]) -> list[_BindingChange]:
    by_intent: dict[tuple[str, str], list[RadarEvidenceRevision]] = defaultdict(list)
    for revision in revisions:
        by_intent[(revision.event_id, revision.intent_id)].append(revision)
    changes: list[_BindingChange] = []
    for intent_key, intent_revisions in sorted(by_intent.items()):
        del intent_key
        actual_target: TargetKey | None = None
        active_target: TargetKey | None = None
        for revision in sorted(intent_revisions, key=_revision_order_key):
            new_target = _resolved_target(revision)
            if new_target == actual_target:
                continue
            effective_at_ms = max(revision.event_created_at_ms, revision.resolution_created_at_ms)
            event = _EvidenceEvent(
                event_id=revision.event_id,
                source_event_at_ms=revision.source_event_at_ms,
                author_key=revision.author_key,
                text_key=revision.text_fingerprint or _MISSING_TEXT_KEY,
            )
            if active_target is not None:
                changes.append(
                    _BindingChange(
                        effective_at_ms=effective_at_ms,
                        delta=-1,
                        event=event,
                        target=active_target,
                        intent_id=revision.intent_id,
                        resolution_id=revision.resolution_id,
                    )
                )
                active_target = None
            actual_target = new_target
            if new_target is not None and _resolution_is_timely(revision):
                changes.append(
                    _BindingChange(
                        effective_at_ms=effective_at_ms,
                        delta=1,
                        event=event,
                        target=new_target,
                        intent_id=revision.intent_id,
                        resolution_id=revision.resolution_id,
                    )
                )
                active_target = new_target
    return sorted(changes, key=_binding_change_sort_key)


def _replay(
    events: Mapping[str, _EvidenceEvent],
    changes: Sequence[_BindingChange],
    *,
    replay_start_ms: int,
    now_ms: int,
    deadline_monotonic: float | None,
) -> dict[TargetKey, _TargetState]:
    changes_by_time: dict[int, list[_BindingChange]] = defaultdict(list)
    for change in changes:
        if change.effective_at_ms <= now_ms:
            changes_by_time[change.effective_at_ms].append(change)
    boundaries_by_time: dict[int, list[tuple[str, _EvidenceEvent]]] = defaultdict(list)
    for event in events.values():
        current_to_prior_at = event.source_event_at_ms + TOKEN_RADAR_CURRENT_WINDOW_MS + 1
        prior_to_out_at = event.source_event_at_ms + TOKEN_RADAR_CURRENT_WINDOW_MS + TOKEN_RADAR_PRIOR_WINDOW_MS
        if replay_start_ms < current_to_prior_at <= now_ms:
            boundaries_by_time[current_to_prior_at].append(("to_prior", event))
        if replay_start_ms < prior_to_out_at <= now_ms:
            boundaries_by_time[prior_to_out_at].append(("to_out", event))

    binding_counts: dict[BindingKey, int] = {}
    active_targets_by_event: dict[str, set[TargetKey]] = defaultdict(set)
    for effective_at_ms in sorted(time_key for time_key in changes_by_time if time_key < replay_start_ms):
        _apply_binding_group(
            changes_by_time[effective_at_ms],
            binding_counts=binding_counts,
            active_targets_by_event=active_targets_by_event,
        )

    states: dict[TargetKey, _TargetState] = {}
    for (event_id, target), count in sorted(binding_counts.items()):
        if count <= 0:
            continue
        state = states.setdefault(target, _TargetState())
        state.add_evidence(events[event_id], at_ms=replay_start_ms)
    for state in states.values():
        state.gate_is_true = state.gate()

    transition_times = sorted(
        {
            *(time_key for time_key in changes_by_time if replay_start_ms <= time_key <= now_ms),
            *boundaries_by_time,
        }
    )
    for index, effective_at_ms in enumerate(transition_times):
        if index % 128 == 0:
            _check_deadline(deadline_monotonic)
        removals, additions = _apply_binding_group(
            changes_by_time.get(effective_at_ms, ()),
            binding_counts=binding_counts,
            active_targets_by_event=active_targets_by_event,
        )
        negative_targets: set[TargetKey] = set()
        for boundary_kind, event in sorted(
            boundaries_by_time.get(effective_at_ms, ()),
            key=lambda item: (item[0], item[1].event_id),
        ):
            for target in sorted(active_targets_by_event.get(event.event_id, ())):
                state = states.setdefault(target, _TargetState())
                changed = (
                    state.move_current_to_prior(event.event_id)
                    if boundary_kind == "to_prior"
                    else state.remove_prior(event.event_id)
                )
                if changed:
                    negative_targets.add(target)
        for change in removals:
            state = states.setdefault(change.target, _TargetState())
            if state.remove_evidence(change.event.event_id):
                negative_targets.add(change.target)
        for target in negative_targets:
            state = states[target]
            state.expire_episode(at_ms=effective_at_ms)
            state.gate_is_true = state.gate()
            if not state.gate_is_true:
                state.episode = None

        positive_by_target: dict[TargetKey, list[_BindingChange]] = defaultdict(list)
        for change in additions:
            state = states.setdefault(change.target, _TargetState())
            if state.add_evidence(change.event, at_ms=effective_at_ms):
                positive_by_target[change.target].append(change)
        for target, target_additions in sorted(positive_by_target.items()):
            state = states[target]
            state.expire_episode(at_ms=effective_at_ms)
            gate_before = state.gate_is_true
            gate_after = state.gate()
            if not gate_after:
                state.episode = None
            elif not gate_before:
                state.episode = _Episode(
                    qualified_at_ms=effective_at_ms,
                    trigger=min(target_additions, key=_binding_change_sort_key),
                )
            state.gate_is_true = gate_after
    return states


def _apply_binding_group(
    changes: Sequence[_BindingChange],
    *,
    binding_counts: dict[BindingKey, int],
    active_targets_by_event: dict[str, set[TargetKey]],
) -> tuple[list[_BindingChange], list[_BindingChange]]:
    by_binding: dict[BindingKey, list[_BindingChange]] = defaultdict(list)
    for change in changes:
        by_binding[change.binding_key].append(change)
    removals: list[_BindingChange] = []
    additions: list[_BindingChange] = []
    for binding_key, binding_changes in sorted(by_binding.items()):
        before = binding_counts.get(binding_key, 0)
        after = before + sum(change.delta for change in binding_changes)
        if after < 0:
            raise TokenRadarInvariantViolation("token_radar_binding_refcount_invalid")
        event_id, target = binding_key
        if before > 0 and after == 0:
            removals.append(min(binding_changes, key=_binding_change_sort_key))
            active_targets_by_event[event_id].discard(target)
        elif before == 0 and after > 0:
            positive = [change for change in binding_changes if change.delta > 0]
            if not positive:
                raise TokenRadarInvariantViolation("token_radar_binding_addition_missing")
            additions.append(min(positive, key=_binding_change_sort_key))
            active_targets_by_event[event_id].add(target)
        if after == 0:
            binding_counts.pop(binding_key, None)
        else:
            binding_counts[binding_key] = after
    return removals, additions


def _public_identity(target: TargetKey, fact: Mapping[str, Any]) -> dict[str, Any]:
    symbol = _text(fact.get("symbol"))
    chain = _text(fact.get("chain"))
    exchange = _text(fact.get("exchange"))
    address = _text(fact.get("address"))
    if symbol is None and target[0] == "Asset" and address is not None:
        symbol = address
    if symbol is None:
        raise TokenRadarInvariantViolation("token_radar_identity_symbol_missing")
    if target[0] == "Asset":
        if chain is None or address is None or exchange is not None:
            raise TokenRadarInvariantViolation("token_radar_asset_identity_invalid")
    elif exchange is None or chain is not None or address is not None:
        raise TokenRadarInvariantViolation("token_radar_cex_identity_invalid")
    return {
        "target_type": target[0],
        "target_id": target[1],
        "symbol": symbol,
        "name": _text(fact.get("name")),
        "logo_url": _local_logo_url(fact.get("logo_url")),
        "chain": chain,
        "exchange": exchange,
        "address": address,
    }


def _presentation_market(
    fact: Mapping[str, Any],
    *,
    signal_price_usd: Any,
    trigger_source_event_at_ms: int,
    now_ms: int,
) -> dict[str, Any]:
    price_observed_at_ms = _fresh_observed_at_ms(fact.get("price_observed_at_ms"), now_ms=now_ms)
    market_cap_observed_at_ms = _fresh_observed_at_ms(
        fact.get("market_cap_observed_at_ms"),
        now_ms=now_ms,
    )
    price = _positive_decimal(fact.get("price_usd")) if price_observed_at_ms is not None else None
    market_cap = _positive_decimal(fact.get("market_cap_usd")) if market_cap_observed_at_ms is not None else None
    if price is None:
        price_observed_at_ms = None
    if market_cap is None:
        market_cap_observed_at_ms = None
    signal = _positive_decimal(signal_price_usd)
    change: float | None = None
    if (
        signal is not None
        and price is not None
        and price_observed_at_ms is not None
        and price_observed_at_ms >= trigger_source_event_at_ms
    ):
        candidate_change = float((price - signal) / signal)
        change = candidate_change if math.isfinite(candidate_change) else None
    return {
        "price_usd": float(price) if price is not None else None,
        "price_observed_at_ms": price_observed_at_ms,
        "price_change_since_signal": change,
        "market_cap_usd": float(market_cap) if market_cap is not None else None,
        "market_cap_observed_at_ms": market_cap_observed_at_ms,
    }


def _unavailable_market() -> dict[str, Any]:
    return {
        "price_usd": None,
        "price_observed_at_ms": None,
        "price_change_since_signal": None,
        "market_cap_usd": None,
        "market_cap_observed_at_ms": None,
    }


def _fresh_observed_at_ms(value: Any, *, now_ms: int) -> int | None:
    observed_at_ms = _optional_nonnegative_int(value)
    if observed_at_ms is None or observed_at_ms > now_ms or now_ms - observed_at_ms > LIVE_MARKET_STALE_AFTER_MS:
        return None
    return observed_at_ms


def _canonical_presentation_fact(raw: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "target_type": _required_text(raw.get("target_type"), "token_radar_target_type_required"),
        "target_id": _required_text(raw.get("target_id"), "token_radar_target_id_required"),
        "symbol": _text(raw.get("symbol")),
        "name": _text(raw.get("name")),
        "logo_url": _local_logo_url(raw.get("logo_url")),
        "chain": _text(raw.get("chain")),
        "exchange": _text(raw.get("exchange")),
        "address": _text(raw.get("address")),
        "signal_price_usd": _decimal_text(raw.get("signal_price_usd")),
        "price_usd": _decimal_text(raw.get("price_usd")),
        "price_observed_at_ms": _optional_nonnegative_int(raw.get("price_observed_at_ms")),
        "market_cap_usd": _decimal_text(raw.get("market_cap_usd")),
        "market_cap_observed_at_ms": _optional_nonnegative_int(raw.get("market_cap_observed_at_ms")),
    }


def _revision_order_key(revision: RadarEvidenceRevision) -> tuple[Any, ...]:
    return (
        revision.resolution_decision_at_ms,
        revision.resolution_created_at_ms,
        revision.resolution_id,
    )


def _binding_change_sort_key(change: _BindingChange) -> tuple[Any, ...]:
    return (
        change.effective_at_ms,
        change.event.event_id,
        change.intent_id,
        change.resolution_id,
        change.target[0],
        change.target[1],
        change.delta,
    )


def _canonical_revision_sort_key(revision: RadarEvidenceRevision) -> tuple[Any, ...]:
    return (
        revision.source_event_at_ms,
        revision.event_id,
        revision.intent_id,
        revision.resolution_decision_at_ms,
        revision.resolution_created_at_ms,
        revision.resolution_id,
    )


def token_radar_text_fingerprint(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = _FINGERPRINT_SPACE_RE.sub(
        " ",
        value.translate(_ASCII_LOWER_TRANSLATION),
    ).strip(" ")
    if not normalized:
        return None
    return hashlib.md5(normalized.encode("utf-8"), usedforsecurity=False).hexdigest()


def _fingerprint(value: Any) -> str:
    return f"sha256:{hashlib.sha256(_canonical_json_bytes(value)).hexdigest()}"


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")


def _local_logo_url(value: Any) -> str | None:
    text = _text(value)
    return text if text is not None and _LOCAL_TOKEN_IMAGE_RE.fullmatch(text) else None


def _normalized_author(value: Any) -> str | None:
    text = _text(value)
    return text.lstrip("@").casefold() or None if text is not None else None


def _decimal_text(value: Any) -> str | None:
    decimal = _positive_decimal(value)
    return format(decimal, "f") if decimal is not None else None


def _positive_decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not parsed.is_finite() or parsed <= 0:
        return None
    return parsed


def _required_text(value: Any, error_code: str) -> str:
    parsed = _text(value)
    if parsed is None:
        raise TokenRadarInvariantViolation(error_code)
    return parsed


def _text(value: Any) -> str | None:
    if value is None:
        return None
    parsed = str(value).strip()
    return parsed or None


def _optional_nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _nonnegative_int(value: Any, error_code: str) -> int:
    parsed = _optional_nonnegative_int(value)
    if parsed is None:
        raise TokenRadarInvariantViolation(error_code)
    return parsed


def _check_deadline(deadline_monotonic: float | None) -> None:
    if deadline_monotonic is not None and time.monotonic() > deadline_monotonic:
        raise TokenRadarBudgetExceeded("token_radar_reducer_budget_exceeded")


__all__ = [
    "TOKEN_RADAR_INPUT_BYTE_CAP",
    "TOKEN_RADAR_INPUT_ROW_CAP",
    "TOKEN_RADAR_OUTPUT_BYTE_CAP",
    "RadarEvidenceRevision",
    "RadarSelectionKey",
    "ReducedTokenRadar",
    "TokenRadarBudgetExceeded",
    "TokenRadarInputOverflow",
    "TokenRadarInvariantViolation",
    "TokenRadarOutputOverflow",
    "enrich_token_radar",
    "reduce_token_radar",
    "token_radar_input_row_size",
    "token_radar_input_size",
    "token_radar_text_fingerprint",
]
