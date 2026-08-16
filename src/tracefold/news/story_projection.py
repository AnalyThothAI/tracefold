"""One deterministic, fact-coherent News Story projection module.

Callers provide one captured material-fact snapshot and receive the complete
desired Item, Story, membership, and public-selection projection.  All Story
identity behavior is private to this module.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Final, cast

import regex
from pyuca import Collator  # type: ignore[import-untyped]

from tracefold.news.classification import SEVERITY_VALUES, classify_by_keyword
from tracefold.news.exact_atom_identity import (
    EventFamily,
    comparison_title,
    decimal_text,
    describe_exact_atom,
    event_window_ms,
)
from tracefold.news.identity import utf16_length, utf16_sort_key
from tracefold.news.models import (
    CLASSIFIER_VERSION,
    IMPORTANCE_VERSION,
    STORY_CLUSTERING_VERSION,
    STORY_COMPARISON_VERSION,
    STORY_EVENT_POLICY_VERSION,
    STORY_FEATURE_VERSION,
    STORY_GROUNDED_PROVIDER_VERSION,
    STORY_IDENTITY_VERSION,
    STORY_JACCARD_VERSION,
    STORY_PROJECTION_VERSION,
    STORY_SELECTOR_VERSION,
    EventCategory,
    ThreatLevel,
)
from tracefold.news.ranking import (
    diplomacy_entity_keys,
    importance_factors,
    promote_diplomacy_severity,
    select_top_stories,
)
from tracefold.news.sources import public_rss_membership_sources, reporting_origin_tier
from tracefold.news.story_store import NewsProjectionInputExceeded, _require_bounded_story_rows

HIGH_JACCARD_NUMERATOR: Final = 4
HIGH_JACCARD_DENOMINATOR: Final = 5
NORMAL_JACCARD_NUMERATOR: Final = 11
NORMAL_JACCARD_DENOMINATOR: Final = 20
MAX_CANDIDATE_BUCKET: Final = 250
MAX_CANDIDATE_PAIRS: Final = 250_000
MAX_TOKENS: Final = 256
MAX_STRONG_VALUES: Final = 16
MAX_REASON_CODES: Final = 32
MAX_IDENTITY_EVIDENCE_BYTES: Final = 8 * 1024
MAX_ITEMS_PER_CATEGORY: Final = 20

_CATEGORY_ORDER: tuple[EventCategory, ...] = (
    "conflict",
    "protest",
    "disaster",
    "diplomatic",
    "economic",
    "terrorism",
    "cyber",
    "health",
    "environmental",
    "military",
    "crime",
    "infrastructure",
    "tech",
    "general",
)
_PUBLIC_TOP_STORY_FIELDS: tuple[str, ...] = (
    "story_id",
    "primary_title",
    "primary_source",
    "primary_link",
    "primary_published_at_ms",
    "source_count",
    "unique_source_count",
    "sources",
    "last_updated_ms",
    "member_titles",
    "source_tier",
    "upstream_importance_score",
    "entity_corroboration",
    "corroboration_source_count",
    "importance_score",
    "effective_importance_score",
    "is_alert",
    "threat_level",
    "category",
)
_PUBLIC_STORY_CATEGORIES: tuple[tuple[tuple[str, ...], str, str], ...] = (
    (("war", "attack", "missile", "troops", "airstrike", "combat", "military"), "conflict", "critical"),
    (("killed", "dead", "casualties", "massacre", "shooting"), "violence", "high"),
    (("protest", "uprising", "riot", "unrest", "coup"), "unrest", "high"),
    (("sanctions", "tensions", "escalation", "threat"), "geopolitical", "elevated"),
    (("crisis", "emergency", "disaster", "collapse"), "crisis", "high"),
    (("earthquake", "flood", "hurricane", "wildfire", "tsunami"), "natural_disaster", "elevated"),
    (("election", "vote", "parliament", "legislation"), "political", "moderate"),
    (("market", "economy", "trade", "tariff", "inflation"), "economic", "moderate"),
)
_PUBLIC_SOURCE_COLLATOR = Collator()

_WORD_RE = regex.compile(r"[\p{L}\p{N}]+(?:['_-][\p{L}\p{N}]+)*")
_CJK_RE = regex.compile(r"^[\p{Han}\p{Hiragana}\p{Katakana}\p{Hangul}]+$")
_NUMBER_RE = re.compile(
    r"(?P<currency>[$€£¥])?\s*(?P<number>\d[\d,]*(?:\.\d+)?)\s*"
    r"(?P<scale>trillion|billion|million|thousand|tn|bn|[tbmk])?\s*(?P<percent>%)?",
    re.IGNORECASE,
)
_PROPER_NAME_RE = re.compile(r"\b(?:[A-Z][A-Za-z0-9&.'-]{1,31})(?:\s+[A-Z][A-Za-z0-9&.'-]{1,31}){0,3}\b")
_UPPER_ASSET_RE = re.compile(r"(?<![A-Za-z0-9])\$?([A-Z][A-Z0-9]{1,9})(?![A-Za-z0-9])")

_ACTION_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("increase", ("raise", "raises", "raised", "increase", "increases", "rise", "rises", "surge", "surges")),
    ("decrease", ("cut", "cuts", "reduce", "reduces", "fall", "falls", "drop", "drops", "decline", "declines")),
    ("acquire", ("acquire", "acquires", "buy", "buys", "purchase", "purchases")),
    ("sell", ("sell", "sells", "sold", "dispose", "disposes")),
    ("file", ("filing", "files", "filed")),
    ("stake", ("stake", "stakes", "shareholding")),
    ("list", ("listing", "lists", "listed")),
    ("delist", ("delisting", "delists", "delisted")),
    ("earnings", ("earnings", "revenue", "profit", "loss", "forecast", "forecasts", "guidance")),
    ("attack", ("attack", "attacks", "strike", "strikes", "airstrike", "bomb", "bombs")),
    ("announce", ("announce", "announces", "announced", "report", "reports", "reported")),
    ("approve", ("approve", "approves", "approved", "authorize", "authorizes")),
    ("reject", ("reject", "rejects", "rejected", "deny", "denies")),
)
_LEXICAL_ALIASES = {form: key for key, forms in _ACTION_GROUPS for form in forms}
_LEXICAL_ALIASES.update({"reached": "reach", "reaches": "reach", "forecasted": "forecast"})
_PROPER_NAME_STOP = frozenset(
    {
        "Alert",
        "Breaking",
        "Developing",
        "Exclusive",
        "Just",
        "New",
        "News",
        "Report",
        "Reuters",
        "SEC",
        "The",
        "Update",
        "Urgent",
        "Whale",
        "Value",
        "Ratio",
        "Rise",
        "Fall",
        "OI",
    }
)
_ASSET_STOP = frozenset(
    {"AI", "AP", "BBC", "CEO", "CFO", "CNN", "CPI", "ETF", "FED", "GDP", "IPO", "OI", "SEC", "US", "USD", "WSJ"}
)
_LEXICAL_STOP = frozenset(
    {
        "a",
        "an",
        "and",
        "as",
        "at",
        "be",
        "by",
        "could",
        "for",
        "from",
        "in",
        "into",
        "of",
        "on",
        "reach",
        "says",
        "the",
        "to",
        "with",
        "will",
    }
)


@dataclass(frozen=True, slots=True)
class NewsStoryFactSnapshot:
    material_snapshot_fingerprint: str
    evaluation_time_ms: int
    published_material_snapshot_fingerprint: str | None
    rows: tuple[dict[str, Any], ...]

    @property
    def unchanged(self) -> bool:
        return self.published_material_snapshot_fingerprint == self.material_snapshot_fingerprint


@dataclass(frozen=True, slots=True)
class NewsStoryProjection:
    material_snapshot_fingerprint: str
    projection_version: str
    projection_fingerprint: str
    versions: dict[str, str]
    population_item_ids: tuple[str, ...]
    item_updates: tuple[dict[str, Any], ...]
    stories: tuple[dict[str, Any], ...]
    memberships: tuple[dict[str, str], ...]
    selection_snapshot: dict[str, Any]
    diagnostics: dict[str, Any]

    def as_payload(self) -> dict[str, Any]:
        return {
            "input_fingerprint": self.material_snapshot_fingerprint,
            "projection_version": self.projection_version,
            "projection_fingerprint": self.projection_fingerprint,
            "versions": dict(self.versions),
            "population_item_ids": list(self.population_item_ids),
            "item_updates": [dict(row) for row in self.item_updates],
            "stories": [dict(row) for row in self.stories],
            "memberships": [dict(row) for row in self.memberships],
            "selection_snapshot": dict(self.selection_snapshot),
            "diagnostics": dict(self.diagnostics),
        }


@dataclass(frozen=True, slots=True)
class _NumericFact:
    kind: str
    value: Decimal

    @property
    def key(self) -> str:
        return f"{self.kind}:{decimal_text(self.value)}"


@dataclass(frozen=True, slots=True)
class _Features:
    row_index: int
    comparison_title: str
    tokens: frozenset[str]
    event_family: EventFamily
    assets: frozenset[str]
    actors: frozenset[str]
    targets: frozenset[str]
    actions: frozenset[str]
    instruments: frozenset[str]
    periods: frozenset[str]
    locations: frozenset[str]
    numbers: tuple[_NumericFact, ...]
    strong_keys: frozenset[str]
    grounded_provider_count: int


@dataclass(slots=True)
class _Atom:
    features: _Features
    row_indices: list[int]
    order: int = 0


@dataclass(frozen=True, slots=True)
class _Decision:
    accepted: bool
    reason: str
    rank: int
    shared_strong: int
    intersection: int
    union: int


@dataclass(frozen=True, slots=True)
class _ClosureDiagnostics:
    exact_atom_count: int
    exact_membership_count: int
    candidate_pair_count: int
    accepted_decision_count: int
    rejected_decision_count: int
    conflict_veto_count: int
    ambiguity_split_count: int
    grounded_provider_count: int
    event_family_counts: dict[str, int]


@dataclass(slots=True)
class _Cluster:
    anchor: _Atom
    atoms: list[_Atom]
    assets: set[str] = field(default_factory=set)
    actors: set[str] = field(default_factory=set)
    targets: set[str] = field(default_factory=set)
    actions: set[str] = field(default_factory=set)
    instruments: set[str] = field(default_factory=set)
    periods: set[str] = field(default_factory=set)
    locations: set[str] = field(default_factory=set)
    numbers: dict[str, set[Decimal]] = field(default_factory=dict)
    accepted_reasons: Counter[str] = field(default_factory=Counter)
    rejected_reasons: Counter[str] = field(default_factory=Counter)
    max_accepted_jaccard: tuple[int, int] = (0, 1)
    max_rejected_jaccard: tuple[int, int] = (0, 1)

    def __post_init__(self) -> None:
        self._add_signature(self.anchor.features)
        self.accepted_reasons["exact_title"] += max(0, len(self.anchor.row_indices) - 1)

    def _add_signature(self, features: _Features) -> None:
        self.assets.update(features.assets)
        self.actors.update(features.actors)
        self.targets.update(features.targets)
        self.actions.update(features.actions)
        self.instruments.update(features.instruments)
        self.periods.update(features.periods)
        self.locations.update(features.locations)
        for fact in features.numbers:
            self.numbers.setdefault(fact.kind, set()).add(fact.value)

    def accept(self, atom: _Atom, decision: _Decision) -> None:
        self.atoms.append(atom)
        self._add_signature(atom.features)
        self.accepted_reasons[decision.reason] += 1
        self.accepted_reasons["exact_title"] += max(0, len(atom.row_indices) - 1)
        if _ratio_greater((decision.intersection, decision.union), self.max_accepted_jaccard):
            self.max_accepted_jaccard = (decision.intersection, decision.union)


@dataclass(slots=True)
class _ProjectedPopulation:
    rows: list[dict[str, Any]]
    clusters: list[_Cluster]
    item_updates: list[dict[str, Any]]
    diagnostics: _ClosureDiagnostics


def build_story_projection(snapshot: NewsStoryFactSnapshot) -> NewsStoryProjection:
    """Return one complete deterministic Story/selection publication."""

    _require_bounded_story_rows(snapshot.rows)
    preliminary_diagnostics: list[_ClosureDiagnostics] = []
    selected_rows = _select_population(
        snapshot.rows,
        now_ms=snapshot.evaluation_time_ms,
        preliminary_diagnostics=preliminary_diagnostics,
    )
    projected = _project_population(selected_rows, now_ms=snapshot.evaluation_time_ms)
    stories, memberships, public_clusters = _materialize_stories(projected)

    selection_stats: dict[str, int | bool] = {}
    selected = select_top_stories(public_clusters, now_ms=snapshot.evaluation_time_ms, stats=selection_stats)
    selection_payload = {
        "projection_revision": snapshot.material_snapshot_fingerprint,
        "selector_evaluated_at_ms": snapshot.evaluation_time_ms,
        "top_stories": [{field: cluster[field] for field in _PUBLIC_TOP_STORY_FIELDS} for cluster in selected],
        "selection_stats": selection_stats,
        "selector_version": STORY_SELECTOR_VERSION,
        "identity_version": STORY_IDENTITY_VERSION,
    }
    selection_snapshot = {
        **selection_payload,
        "selection_fingerprint": _stable_hash(selection_payload),
    }
    versions = {
        "comparison": STORY_COMPARISON_VERSION,
        "feature": STORY_FEATURE_VERSION,
        "grounded_provider": STORY_GROUNDED_PROVIDER_VERSION,
        "event_policy": STORY_EVENT_POLICY_VERSION,
        "jaccard": STORY_JACCARD_VERSION,
        "clustering": STORY_CLUSTERING_VERSION,
        "identity": STORY_IDENTITY_VERSION,
        "classifier": CLASSIFIER_VERSION,
        "importance": IMPORTANCE_VERSION,
        "selector": STORY_SELECTOR_VERSION,
    }
    population_item_ids = tuple(sorted(str(row["item_id"]) for row in projected.rows))
    item_updates = tuple(projected.item_updates)
    ordered_stories = tuple(sorted(stories, key=lambda row: str(row["story_id"])))
    ordered_memberships = tuple(sorted(memberships, key=lambda row: (row["story_id"], row["item_id"])))
    closure_diagnostics = projected.diagnostics
    preliminary_candidate_count = preliminary_diagnostics[0].candidate_pair_count if preliminary_diagnostics else 0
    diagnostics: dict[str, Any] = {
        "input_physical_item_count": len(snapshot.rows),
        "input_encoded_bytes": len(json.dumps(snapshot.rows, ensure_ascii=False, sort_keys=True, default=str).encode()),
        "population_physical_item_count": len(projected.rows),
        "exact_atom_count": closure_diagnostics.exact_atom_count,
        "exact_membership_count": closure_diagnostics.exact_membership_count,
        "candidate_pair_count": closure_diagnostics.candidate_pair_count,
        "preliminary_rss_candidate_pair_count": preliminary_candidate_count,
        "candidate_pair_peak": max(
            closure_diagnostics.candidate_pair_count,
            preliminary_candidate_count,
        ),
        "accepted_decision_count": closure_diagnostics.accepted_decision_count,
        "rejected_decision_count": closure_diagnostics.rejected_decision_count,
        "conflict_veto_count": closure_diagnostics.conflict_veto_count,
        "ambiguity_split_count": closure_diagnostics.ambiguity_split_count,
        "grounded_provider_count": closure_diagnostics.grounded_provider_count,
        "event_family_counts": dict(closure_diagnostics.event_family_counts),
        "story_count": len(ordered_stories),
    }
    desired_state = {
        "material_snapshot_fingerprint": snapshot.material_snapshot_fingerprint,
        "projection_version": STORY_PROJECTION_VERSION,
        "versions": versions,
        "population_item_ids": population_item_ids,
        "item_updates": item_updates,
        "stories": ordered_stories,
        "memberships": ordered_memberships,
        "selection_snapshot": selection_snapshot,
        "diagnostics": diagnostics,
    }
    return NewsStoryProjection(
        material_snapshot_fingerprint=snapshot.material_snapshot_fingerprint,
        projection_version=STORY_PROJECTION_VERSION,
        projection_fingerprint=_stable_hash(desired_state),
        versions=versions,
        population_item_ids=population_item_ids,
        item_updates=item_updates,
        stories=ordered_stories,
        memberships=ordered_memberships,
        selection_snapshot=selection_snapshot,
        diagnostics=diagnostics,
    )


def _select_population(
    rows: Sequence[Mapping[str, Any]],
    *,
    now_ms: int,
    preliminary_diagnostics: list[_ClosureDiagnostics] | None = None,
) -> list[dict[str, Any]]:
    physical_rows = [dict(row) for row in rows]
    rss_rows = [row for row in physical_rows if str(row.get("source_kind")) == "rss"]
    opennews_rows = [row for row in physical_rows if str(row.get("source_kind")) == "opennews"]
    if len(rss_rows) + len(opennews_rows) != len(physical_rows):
        raise RuntimeError("news_projection_source_kind_invalid")

    selected_rss: list[dict[str, Any]] = []
    if rss_rows:
        preliminary = _project_population(rss_rows, now_ms=now_ms)
        if preliminary_diagnostics is not None:
            preliminary_diagnostics.append(preliminary.diagnostics)
        scored_by_item = {str(row["item_id"]): row for row in preliminary.rows}
        source_rows: dict[str, list[dict[str, Any]]] = {}
        for row in rss_rows:
            source_rows.setdefault(str(row["source_id"]), []).append(row)
        for source_group in source_rows.values():
            source_group.sort(
                key=lambda row: (
                    int(row["source_position"]) if row.get("source_position") is not None else 5,
                    str(row["item_id"]),
                )
            )
        by_category: dict[str, list[dict[str, Any]]] = {}
        for category, source in public_rss_membership_sources():
            for row in source_rows.get(source.source_id, ()):  # physical features precede this expansion
                by_category.setdefault(category, []).append(scored_by_item[str(row["item_id"])])
        for category_rows in by_category.values():
            category_rows.sort(
                key=lambda row: (
                    -int(row["importance_score"]),
                    -int(row["published_at_ms"]),
                    str(row["item_id"]),
                )
            )
            selected_rss.extend(category_rows[:MAX_ITEMS_PER_CATEGORY])

    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in [
        *selected_rss,
        *sorted(opennews_rows, key=lambda value: (int(value["published_at_ms"]), str(value["item_id"]))),
    ]:
        item_id = str(row["item_id"])
        if item_id in seen:
            continue
        seen.add(item_id)
        selected.append(dict(row))
    return selected


def _project_population(source_rows: Sequence[Mapping[str, Any]], *, now_ms: int) -> _ProjectedPopulation:
    rows = [dict(row) for row in source_rows]
    features = [_extract_features(row, row_index=index) for index, row in enumerate(rows)]
    clusters, diagnostics = _fixed_anchor_clusters(rows, features)

    story_key_by_row: dict[int, str] = {}
    for cluster_index, cluster in enumerate(clusters):
        for atom in cluster.atoms:
            for row_index in atom.row_indices:
                story_key_by_row[row_index] = str(cluster_index)

    entity_buckets: dict[str, dict[str, set[str]]] = {}
    for index, row in enumerate(rows):
        if int(now_ms) - int(row["published_at_ms"]) > 86_400_000:
            continue
        origin = str(row["reporting_origin"])
        tier = reporting_origin_tier(origin, fallback_tier=int(row["tier"]))
        for entity_key in diplomacy_entity_keys(str(row["title"])):
            bucket = entity_buckets.setdefault(entity_key, {"stories": set(), "origins": set(), "tier12": set()})
            bucket["stories"].add(story_key_by_row[index])
            bucket["origins"].add(origin)
            if tier <= 2:
                bucket["tier12"].add(origin)
    signal_by_story: dict[str, tuple[int, int]] = {}
    for bucket in entity_buckets.values():
        if len(bucket["origins"]) < 2:
            continue
        signal = (len(bucket["origins"]), len(bucket["tier12"]))
        for story_key in bucket["stories"]:
            previous = signal_by_story.get(story_key, (0, 0))
            signal_by_story[story_key] = (max(previous[0], signal[0]), max(previous[1], signal[1]))

    item_updates: list[dict[str, Any]] = []
    for cluster_index, cluster in enumerate(clusters):
        row_indices = [index for atom in cluster.atoms for index in atom.row_indices]
        source_count = len({str(rows[index]["reporting_origin"]) for index in row_indices})
        entity_count, tier12_count = signal_by_story.get(str(cluster_index), (0, 0))
        for index in row_indices:
            row = rows[index]
            classification = classify_by_keyword(str(row["title"]), now_ms=int(now_ms))
            tier = reporting_origin_tier(str(row["reporting_origin"]), fallback_tier=int(row["tier"]))
            level = promote_diplomacy_severity(
                classification.level,
                title=str(row["title"]),
                tier12_origin_count=tier12_count,
            )
            factors = importance_factors(
                level=level,
                tier=tier,
                corroboration_count=source_count,
                published_at_ms=int(row["published_at_ms"]),
                now_ms=int(now_ms),
                title=str(row["title"]),
                entity_corroboration_count=entity_count,
            )
            row.update(
                {
                    "effective_tier": tier,
                    "level": level,
                    "category": classification.category,
                    "classification_source": classification.source,
                    "classification_confidence": classification.confidence,
                    "importance_score": int(factors["total"]),
                    "importance_factors": factors,
                }
            )
            item_updates.append(
                {
                    key: row[key]
                    for key in (
                        "item_id",
                        "level",
                        "category",
                        "classification_source",
                        "classification_confidence",
                        "importance_score",
                        "importance_factors",
                    )
                }
            )
    return _ProjectedPopulation(
        rows=rows,
        clusters=clusters,
        item_updates=item_updates,
        diagnostics=diagnostics,
    )


def _extract_features(row: Mapping[str, Any], *, row_index: int) -> _Features:
    original = str(row.get("title") or "")
    exact_atom = describe_exact_atom(original)
    comparison = exact_atom.comparison_title
    tokens = _lexical_tokens(comparison)
    family = exact_atom.event_family
    actions = _action_keys(comparison)
    assets, provider_markets, grounded_provider_count = _asset_keys(
        original,
        row.get("provider_identity"),
        family=family,
    )
    actors, targets = _actor_target_keys(original, comparison, family=family)
    instruments = _instrument_keys(comparison) | provider_markets
    period_source = unicodedata.normalize("NFKC", original)
    periods = frozenset(
        sorted(
            {
                re.sub(r"\s+", "", match.group(0).casefold())
                for match in re.finditer(
                    r"\bq[1-4]\s*20\d{2}\b|\b(?:fy\s*)?20\d{2}\b",
                    period_source,
                    re.IGNORECASE,
                )
            }
        )[:MAX_STRONG_VALUES]
    )
    locations = _location_keys(comparison, family=family)
    numbers = _numeric_facts(comparison, family=family)
    strong_keys = frozenset(
        [
            *(f"asset:{value}" for value in assets),
            *(f"actor:{value}" for value in actors),
            *(f"target:{value}" for value in targets),
            *(f"action:{value}" for value in actions),
            *(f"instrument:{value}" for value in instruments),
            *(f"period:{value}" for value in periods),
            *(f"location:{value}" for value in locations),
            *(f"number:{value.key}" for value in numbers),
        ]
    )
    return _Features(
        row_index=row_index,
        comparison_title=comparison,
        tokens=tokens,
        event_family=family,
        assets=assets,
        actors=actors,
        targets=targets,
        actions=actions,
        instruments=instruments,
        periods=periods,
        locations=locations,
        numbers=numbers,
        strong_keys=strong_keys,
        grounded_provider_count=grounded_provider_count,
    )


def _lexical_tokens(comparison: str) -> frozenset[str]:
    result: set[str] = set()
    for match in _WORD_RE.finditer(comparison):
        token = _LEXICAL_ALIASES.get(match.group(0), match.group(0))
        if _CJK_RE.fullmatch(token):
            if len(token) == 1:
                result.add(token)
            else:
                result.update(token[index : index + 2] for index in range(len(token) - 1))
        elif (len(token) >= 2 or token[0].isdigit()) and token not in _LEXICAL_STOP:
            result.add(token)
        if len(result) >= MAX_TOKENS:
            break
    return frozenset(sorted(result)[:MAX_TOKENS])


def _action_keys(comparison: str) -> frozenset[str]:
    words = set(comparison.split())
    return frozenset(key for key, forms in _ACTION_GROUPS if any(form in words for form in forms))


def _asset_keys(
    original: str,
    provider_identity: object,
    *,
    family: EventFamily,
) -> tuple[frozenset[str], frozenset[str], int]:
    result: set[str] = set()
    markets: set[str] = set()
    grounded = 0
    if isinstance(provider_identity, Sequence) and not isinstance(provider_identity, (str, bytes)):
        for raw in provider_identity[:MAX_STRONG_VALUES]:
            if not isinstance(raw, Mapping):
                continue
            symbol = str(raw.get("symbol") or "").strip().upper()[:32]
            match_text = str(raw.get("match") or "").strip()[:64]
            symbol_grounded = (
                bool(symbol)
                and re.search(rf"(?<![A-Za-z0-9])\$?{re.escape(symbol)}(?![A-Za-z0-9])", original, re.IGNORECASE)
                is not None
            )
            match_grounded = bool(match_text) and match_text.casefold() in original.casefold()
            if symbol and (symbol_grounded or match_grounded):
                result.add(symbol)
                market = _provider_market_key(str(raw.get("market_type") or ""))
                if market:
                    markets.add(market)
                grounded += 1
    if family == "market_telemetry":
        for symbol in _UPPER_ASSET_RE.findall(original):
            if symbol not in _ASSET_STOP:
                result.add(symbol)
    return (
        frozenset(sorted(result)[:MAX_STRONG_VALUES]),
        frozenset(sorted(markets)[:MAX_STRONG_VALUES]),
        grounded,
    )


def _provider_market_key(value: str) -> str | None:
    normalized = re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")
    aliases = {
        "cex": "cex",
        "dex": "dex",
        "future": "futures",
        "futures": "futures",
        "option": "option",
        "options": "option",
        "perp": "perpetual",
        "perpetual": "perpetual",
        "spot": "spot",
    }
    return aliases.get(normalized)


def _actor_target_keys(original: str, comparison: str, *, family: EventFamily) -> tuple[frozenset[str], frozenset[str]]:
    actors: set[str] = set()
    targets: set[str] = set()
    proper_names = [name for name in _PROPER_NAME_RE.findall(original) if name not in _PROPER_NAME_STOP]
    if family == "filing":
        filing = re.search(
            r"^(?P<actor>.+?)\s+(?:raises?|increases?|cuts?|reduces?|acquires?|buys?|sells?|files?)\b.*?\b(?:in|of|for)\s+(?P<target>.+?)(?:\s+to\s+|\s+by\s+|\s*[-–—]\s*sec\b|$)",
            original,
            re.IGNORECASE,
        )
        if filing:
            actors.add(_fact_phrase(filing.group("actor")))
            targets.add(_fact_phrase(filing.group("target")))
        elif proper_names:
            actors.add(_fact_phrase(proper_names[0]))
            targets.update(_fact_phrase(name) for name in proper_names[1:3])
    else:
        actors.update(_fact_phrase(name) for name in proper_names[:3])
    actors.discard("")
    targets.discard("")
    return frozenset(sorted(actors)[:MAX_STRONG_VALUES]), frozenset(sorted(targets)[:MAX_STRONG_VALUES])


def _fact_phrase(value: str) -> str:
    return " ".join(_WORD_RE.findall(comparison_title(value)))[:160]


def _instrument_keys(comparison: str) -> frozenset[str]:
    aliases = {
        "bond": "bond",
        "bonds": "bond",
        "etf": "etf",
        "future": "futures",
        "futures": "futures",
        "option": "option",
        "options": "option",
        "perpetual": "perpetual",
        "share": "shares",
        "shares": "shares",
        "spot": "spot",
        "stake": "stake",
        "stock": "stock",
    }
    instruments = {
        canonical for value, canonical in aliases.items() if re.search(rf"\b{re.escape(value)}\b", comparison)
    }
    return frozenset(instruments)


def _location_keys(comparison: str, *, family: EventFamily) -> frozenset[str]:
    if family != "disaster":
        return frozenset()
    match = regex.search(r"\b(?:in|near|off|strikes?)\s+([\p{L}\s-]{2,80})", comparison, regex.IGNORECASE)
    if not match:
        return frozenset()
    words = [word for word in _WORD_RE.findall(match.group(1)) if word not in {"after", "as", "with"}]
    return frozenset(words[-4:])


def _numeric_facts(comparison: str, *, family: EventFamily) -> tuple[_NumericFact, ...]:
    facts: list[_NumericFact] = []
    words = comparison.split()
    for index, word in enumerate(words):
        match = re.fullmatch(r"(?P<kind>pct|usd|eur|gbp|cny|num)_(?P<value>-?\d+(?:\.\d+)?)", word)
        if not match:
            continue
        kind = match.group("kind")
        if family == "disaster" and index > 0 and words[index - 1] in {"magnitude", "mag"}:
            kind = "magnitude"
        elif family == "market_telemetry":
            nearby = " ".join(words[max(0, index - 3) : index + 1])
            if "ratio" in nearby:
                kind = "ratio"
            elif "value" in nearby:
                kind = "oi_value"
            elif kind == "pct":
                kind = "oi_change_pct"
        facts.append(_NumericFact(kind=kind, value=Decimal(match.group("value"))))
    unique = {(fact.kind, fact.value): fact for fact in facts}
    return tuple(sorted(unique.values(), key=lambda fact: (fact.kind, fact.value))[:MAX_STRONG_VALUES])


def _fixed_anchor_clusters(
    rows: Sequence[Mapping[str, Any]],
    features: Sequence[_Features],
) -> tuple[list[_Cluster], _ClosureDiagnostics]:
    exact: dict[str, list[int]] = {}
    untrackable: list[int] = []
    for feature in features:
        if feature.comparison_title:
            exact.setdefault(feature.comparison_title, []).append(feature.row_index)
        else:
            untrackable.append(feature.row_index)
    atoms: list[_Atom] = []
    for indices in exact.values():
        ordered = sorted(indices, key=lambda index: (int(rows[index]["published_at_ms"]), str(rows[index]["item_id"])))
        chunks: list[list[int]] = []
        for index in ordered:
            if not chunks:
                chunks.append([index])
                continue
            first_index = chunks[-1][0]
            window = event_window_ms(features[first_index].event_family)
            if int(rows[index]["published_at_ms"]) - int(rows[first_index]["published_at_ms"]) <= window:
                chunks[-1].append(index)
            else:
                chunks.append([index])
        atoms.extend(_exact_atom(features, chunk) for chunk in chunks)
    atoms.extend(_Atom(features=features[index], row_indices=[index]) for index in untrackable)
    atoms.sort(
        key=lambda atom: (
            int(rows[atom.row_indices[0]]["published_at_ms"]),
            utf16_sort_key(atom.features.comparison_title),
            str(rows[atom.row_indices[0]]["item_id"]),
        )
    )
    for order, atom in enumerate(atoms):
        atom.order = order
    candidate_pairs = _candidate_pairs(atoms)

    clusters: list[_Cluster] = []
    accepted_decision_count = 0
    rejected_decision_count = 0
    conflict_veto_count = 0
    ambiguity_split_count = 0
    for atom in atoms:
        accepted: list[tuple[_Cluster, _Decision]] = []
        for cluster in clusters:
            pair = (min(cluster.anchor.order, atom.order), max(cluster.anchor.order, atom.order))
            if pair not in candidate_pairs:
                continue
            decision = _same_event(cluster, atom, rows)
            if decision.accepted:
                accepted.append((cluster, decision))
                accepted_decision_count += 1
            else:
                rejected_decision_count += 1
                if decision.reason.endswith("_conflict"):
                    conflict_veto_count += 1
                cluster.rejected_reasons[decision.reason] += 1
                if _ratio_greater((decision.intersection, decision.union), cluster.max_rejected_jaccard):
                    cluster.max_rejected_jaccard = (decision.intersection, decision.union)
        if not accepted:
            clusters.append(_Cluster(anchor=atom, atoms=[atom]))
            continue
        accepted.sort(key=lambda entry: _decision_sort_key(entry[1]), reverse=True)
        best_key = _decision_sort_key(accepted[0][1])
        if len(accepted) > 1 and _decision_sort_key(accepted[1][1]) == best_key:
            singleton = _Cluster(anchor=atom, atoms=[atom])
            singleton.rejected_reasons["ambiguous_anchor_tie"] += 1
            clusters.append(singleton)
            ambiguity_split_count += 1
            continue
        accepted[0][0].accept(atom, accepted[0][1])
    return clusters, _ClosureDiagnostics(
        exact_atom_count=len(atoms),
        exact_membership_count=sum(max(0, len(atom.row_indices) - 1) for atom in atoms),
        candidate_pair_count=len(candidate_pairs),
        accepted_decision_count=accepted_decision_count,
        rejected_decision_count=rejected_decision_count,
        conflict_veto_count=conflict_veto_count,
        ambiguity_split_count=ambiguity_split_count,
        grounded_provider_count=sum(feature.grounded_provider_count for feature in features),
        event_family_counts=dict(sorted(Counter(feature.event_family for feature in features).items())),
    )


def _exact_atom(features: Sequence[_Features], row_indices: list[int]) -> _Atom:
    anchor = features[row_indices[0]]
    members = [features[index] for index in row_indices]
    numbers = {(fact.kind, fact.value): fact for member in members for fact in member.numbers}
    assets = _bounded_feature_union(member.assets for member in members)
    actors = _bounded_feature_union(member.actors for member in members)
    targets = _bounded_feature_union(member.targets for member in members)
    actions = _bounded_feature_union(member.actions for member in members)
    instruments = _bounded_feature_union(member.instruments for member in members)
    periods = _bounded_feature_union(member.periods for member in members)
    locations = _bounded_feature_union(member.locations for member in members)
    bounded_numbers = tuple(sorted(numbers.values(), key=lambda fact: (fact.kind, fact.value))[:MAX_STRONG_VALUES])
    strong_keys = frozenset(
        [
            *(f"asset:{value}" for value in assets),
            *(f"actor:{value}" for value in actors),
            *(f"target:{value}" for value in targets),
            *(f"action:{value}" for value in actions),
            *(f"instrument:{value}" for value in instruments),
            *(f"period:{value}" for value in periods),
            *(f"location:{value}" for value in locations),
            *(f"number:{value.key}" for value in bounded_numbers),
        ]
    )
    return _Atom(
        features=_Features(
            row_index=anchor.row_index,
            comparison_title=anchor.comparison_title,
            tokens=frozenset(value for member in members for value in member.tokens),
            event_family=anchor.event_family,
            assets=assets,
            actors=actors,
            targets=targets,
            actions=actions,
            instruments=instruments,
            periods=periods,
            locations=locations,
            numbers=bounded_numbers,
            strong_keys=strong_keys,
            grounded_provider_count=sum(member.grounded_provider_count for member in members),
        ),
        row_indices=row_indices,
    )


def _bounded_feature_union(values: Iterable[frozenset[str]]) -> frozenset[str]:
    return frozenset(sorted(value for group in values for value in group)[:MAX_STRONG_VALUES])


def _candidate_pairs(atoms: Sequence[_Atom]) -> set[tuple[int, int]]:
    token_index: dict[str, list[int]] = {}
    strong_index: dict[str, list[int]] = {}
    for atom in atoms:
        for token in atom.features.tokens:
            token_index.setdefault(token, []).append(atom.order)
        for key in atom.features.strong_keys:
            strong_index.setdefault(key, []).append(atom.order)
    pairs: set[tuple[int, int]] = set()
    for index in (token_index, strong_index):
        for bucket in index.values():
            if index is token_index and len(bucket) > MAX_CANDIDATE_BUCKET:
                continue
            for position, left in enumerate(bucket):
                for right in bucket[position + 1 :]:
                    pairs.add((left, right))
                    if len(pairs) > MAX_CANDIDATE_PAIRS:
                        raise NewsProjectionInputExceeded("news_story_candidate_pair_cap")
    return pairs


def _same_event(cluster: _Cluster, atom: _Atom, rows: Sequence[Mapping[str, Any]]) -> _Decision:
    anchor = cluster.anchor.features
    candidate = atom.features
    intersection = len(anchor.tokens & candidate.tokens)
    union = len(anchor.tokens | candidate.tokens) or 1
    shared_strong = len(anchor.strong_keys & candidate.strong_keys)
    rejected = _compatibility_rejection(cluster, atom, rows)
    if rejected is not None:
        return _Decision(False, rejected, 0, shared_strong, intersection, union)
    if anchor.comparison_title and anchor.comparison_title == candidate.comparison_title:
        return _Decision(True, "exact_title", 3, shared_strong, intersection, union)
    if intersection * HIGH_JACCARD_DENOMINATOR >= union * HIGH_JACCARD_NUMERATOR:
        return _Decision(True, "high_jaccard", 2, shared_strong, intersection, union)
    if shared_strong and intersection * NORMAL_JACCARD_DENOMINATOR >= union * NORMAL_JACCARD_NUMERATOR:
        return _Decision(True, "strong_signature_jaccard", 1, shared_strong, intersection, union)
    return _Decision(False, "jaccard_below_threshold", 0, shared_strong, intersection, union)


def _compatibility_rejection(cluster: _Cluster, atom: _Atom, rows: Sequence[Mapping[str, Any]]) -> str | None:
    anchor = cluster.anchor.features
    candidate = atom.features
    family = anchor.event_family if anchor.event_family != "general" else candidate.event_family
    if anchor.event_family != candidate.event_family and "general" not in {anchor.event_family, candidate.event_family}:
        return "event_family_conflict"
    anchor_time = int(rows[cluster.anchor.row_indices[0]]["published_at_ms"])
    candidate_time = int(rows[atom.row_indices[0]]["published_at_ms"])
    window = event_window_ms(family)
    if abs(candidate_time - anchor_time) > window:
        return "event_time_conflict"
    for existing, incoming, reason in (
        (cluster.assets, candidate.assets, "asset_conflict"),
        (cluster.instruments, candidate.instruments, "instrument_conflict"),
        (cluster.periods, candidate.periods, "period_conflict"),
    ):
        if existing and incoming and existing.isdisjoint(incoming):
            return reason
    if _actions_conflict(cluster.actions, candidate.actions):
        return "action_conflict"
    if cluster.actors and candidate.actors and cluster.actors.isdisjoint(candidate.actors):
        return "actor_conflict"
    if family == "filing" and cluster.targets and candidate.targets and cluster.targets.isdisjoint(candidate.targets):
        return "target_conflict"
    if (
        family == "disaster"
        and cluster.locations
        and candidate.locations
        and cluster.locations.isdisjoint(candidate.locations)
    ):
        return "location_conflict"
    candidate_numbers: dict[str, set[Decimal]] = {}
    for fact in candidate.numbers:
        candidate_numbers.setdefault(fact.kind, set()).add(fact.value)
    for kind, existing_values in cluster.numbers.items():
        incoming_values = candidate_numbers.get(kind)
        if not incoming_values:
            continue
        if kind == "magnitude" and any(
            abs(left - right) <= Decimal("0.5") for left in existing_values for right in incoming_values
        ):
            continue
        if existing_values.isdisjoint(incoming_values):
            return "numeric_conflict"
    return None


def _actions_conflict(existing: set[str], incoming: frozenset[str]) -> bool:
    if not existing or not incoming:
        return False
    opposites = (
        ({"increase"}, {"decrease"}),
        ({"acquire"}, {"sell"}),
        ({"list"}, {"delist"}),
        ({"approve"}, {"reject"}),
    )
    if any(
        (left & existing and right & incoming) or (right & existing and left & incoming) for left, right in opposites
    ):
        return True
    material_existing = existing - {"announce"}
    material_incoming = set(incoming) - {"announce"}
    return bool(material_existing and material_incoming and material_existing.isdisjoint(material_incoming))


def _decision_sort_key(decision: _Decision) -> tuple[int, int, int, int]:
    return (decision.rank, decision.shared_strong, decision.intersection * 1_000_000 // decision.union, -decision.union)


def _materialize_stories(
    projected: _ProjectedPopulation,
) -> tuple[list[dict[str, Any]], list[dict[str, str]], list[dict[str, Any]]]:
    stories: list[dict[str, Any]] = []
    memberships: list[dict[str, str]] = []
    public_clusters: list[dict[str, Any]] = []
    for cluster in projected.clusters:
        row_indices = [index for atom in cluster.atoms for index in atom.row_indices]
        members = [projected.rows[index] for index in row_indices]
        anchor_row = projected.rows[cluster.anchor.row_indices[0]]
        comparison_identity = cluster.anchor.features.comparison_title
        anchor_item_id = str(anchor_row["item_id"])
        story_id = hashlib.sha256(
            f"{STORY_IDENTITY_VERSION}\0{comparison_identity}\0{anchor_item_id}".encode()
        ).hexdigest()
        representative = min(
            members,
            key=lambda member: (
                int(member["effective_tier"]),
                -int(member["published_at_ms"]),
                str(member["item_id"]),
            ),
        )
        scoring = min(
            members,
            key=lambda member: (
                -int(member["importance_score"]),
                int(member["effective_tier"]),
                -int(member["published_at_ms"]),
                str(member["reporting_origin"]),
                str(member["item_id"]),
            ),
        )
        level = cast(
            ThreatLevel,
            max(
                (str(member["level"]) for member in members),
                key=lambda value: (SEVERITY_VALUES[cast(ThreatLevel, value)], value),
            ),
        )
        category = cast(EventCategory, _mode([str(member["category"]) for member in members], _CATEGORY_ORDER))
        origins = {str(member["reporting_origin"]) for member in members}
        evidence = _identity_evidence(cluster, anchor_item_id=anchor_item_id)
        story = {
            "story_id": story_id,
            "canonical_title": str(anchor_row["title"]),
            "representative_item_id": str(representative["item_id"]),
            "representative_source_id": str(representative["source_id"]),
            "representative_title": str(representative["title"]),
            "representative_url": representative.get("canonical_url"),
            "representative_description": str(representative["description"]),
            "scoring_item_id": str(scoring["item_id"]),
            "level": level,
            "category": category,
            "importance_score": int(scoring["importance_score"]),
            "importance_factors": dict(scoring["importance_factors"]),
            "facet_facts": {
                "source_ids": sorted({str(member["source_id"]) for member in members}),
                "reporting_origins": sorted({origin.strip() for origin in origins if origin.strip()}),
            },
            "identity_evidence": evidence,
            "item_count": len(members),
            "source_count": len(origins),
            "first_published_at_ms": min(int(member["published_at_ms"]) for member in members),
            "last_published_at_ms": max(int(member["published_at_ms"]) for member in members),
        }
        story["state_fingerprint"] = _stable_hash(story)
        stories.append(story)
        memberships.extend({"story_id": story_id, "item_id": str(member["item_id"])} for member in members)

        public_members = [member for member in members if utf16_length(str(member["title"])) > 10]
        if not public_members:
            continue
        public_representative = min(
            public_members,
            key=lambda member: (int(member["effective_tier"]), -int(member["published_at_ms"]), str(member["item_id"])),
        )
        tier_by_origin: dict[str, int] = {}
        for member in members:
            origin = str(member["reporting_origin"]).strip()
            if origin:
                tier_by_origin[origin] = min(
                    tier_by_origin.get(origin, int(member["effective_tier"])), int(member["effective_tier"])
                )
        ordered_origins = sorted(
            tier_by_origin, key=lambda origin: (tier_by_origin[origin], _PUBLIC_SOURCE_COLLATOR.sort_key(origin))
        )
        public_category, public_threat = _categorize_public_story(str(public_representative["title"]))
        public_clusters.append(
            {
                "story_id": story_id,
                "primary_title": str(public_representative["title"]),
                "primary_source": str(public_representative["reporting_origin"]).strip(),
                "primary_link": public_representative.get("canonical_url"),
                "primary_published_at_ms": int(public_representative["published_at_ms"]),
                "source_count": len(ordered_origins),
                "unique_source_count": len(ordered_origins),
                "sources": ordered_origins,
                "last_updated_ms": max(int(member["published_at_ms"]) for member in members),
                "member_titles": [str(member["title"]) for member in members if str(member["title"])],
                "source_tier": min(tier_by_origin.values(), default=4),
                "upstream_importance_score": max(int(member["importance_score"]) for member in members),
                "entity_corroboration": False,
                "corroboration_source_count": 0,
                "is_alert": any(str(member["level"]) in {"critical", "high"} for member in members),
                "threat_level": public_threat,
                "category": public_category,
                "threat": {
                    "level": str(public_representative["level"]),
                    "category": str(public_representative["category"]),
                    "source": str(public_representative["classification_source"]),
                },
            }
        )
    return stories, memberships, public_clusters


def _identity_evidence(cluster: _Cluster, *, anchor_item_id: str) -> dict[str, Any]:
    strong_entities = sorted(
        {
            *(f"asset:{value}" for value in cluster.assets),
            *(f"actor:{value}" for value in cluster.actors),
            *(f"target:{value}" for value in cluster.targets),
        }
    )[:MAX_STRONG_VALUES]
    numeric = sorted(f"{kind}:{decimal_text(value)}" for kind, values in cluster.numbers.items() for value in values)[
        :MAX_STRONG_VALUES
    ]
    evidence = {
        "identity_version": STORY_IDENTITY_VERSION,
        "comparison_version": STORY_COMPARISON_VERSION,
        "feature_version": STORY_FEATURE_VERSION,
        "grounded_provider_version": STORY_GROUNDED_PROVIDER_VERSION,
        "jaccard_version": STORY_JACCARD_VERSION,
        "event_policy_version": STORY_EVENT_POLICY_VERSION,
        "clustering_version": STORY_CLUSTERING_VERSION,
        "anchor_item_id": anchor_item_id,
        "strong_entity_keys": strong_entities,
        "action_keys": sorted(cluster.actions)[:MAX_STRONG_VALUES],
        "numeric_keys": numeric,
        "location_keys": sorted(cluster.locations)[:MAX_STRONG_VALUES],
        "membership_reasons": dict(sorted(cluster.accepted_reasons.items())[:MAX_REASON_CODES]),
        "rejection_reasons": dict(sorted(cluster.rejected_reasons.items())[:MAX_REASON_CODES]),
        "max_accepted_jaccard": _ratio_float(cluster.max_accepted_jaccard),
        "max_rejected_jaccard": _ratio_float(cluster.max_rejected_jaccard),
        "grounded_provider_count": sum(atom.features.grounded_provider_count for atom in cluster.atoms),
    }
    if (
        len(json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())
        > MAX_IDENTITY_EVIDENCE_BYTES
    ):
        raise NewsProjectionInputExceeded("news_story_identity_evidence_byte_cap")
    return evidence


def _ratio_greater(left: tuple[int, int], right: tuple[int, int]) -> bool:
    return left[0] * right[1] > right[0] * left[1]


def _ratio_float(value: tuple[int, int]) -> float:
    return round(value[0] / value[1], 6) if value[1] else 0.0


def _stable_hash(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _mode(values: Sequence[str], order: Sequence[str]) -> str:
    counts = Counter(values)
    highest = max(counts.values())
    index = {value: position for position, value in enumerate(order)}
    return min(
        (value for value, count in counts.items() if count == highest),
        key=lambda value: (index.get(value, len(index)), value),
    )


def _categorize_public_story(title: str) -> tuple[str, str]:
    lowered = title.lower()
    for keywords, category, threat_level in _PUBLIC_STORY_CATEGORIES:
        if any(keyword in lowered for keyword in keywords):
            return category, threat_level
    return "general", "moderate"


__all__ = [
    "STORY_IDENTITY_VERSION",
    "STORY_PROJECTION_VERSION",
    "NewsStoryFactSnapshot",
    "NewsStoryProjection",
    "build_story_projection",
]
