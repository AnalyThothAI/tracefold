"""Storyline keys from a code-owned registry (pure); the status window itself is a repository query.

#509 replaced the ordered regex lexicon with `storyline_registry.json`: conflict / actor / geo / topic entries
carrying literal multi-script aliases. The two behaviors the regexes could not give are the point of the change.
First, *coverage is data*: a missing storyline is one registry row plus one assertion, not a new pattern wedged
into an order-sensitive tuple — the v3 lexicon still dropped 26-28% of a live day into one `macro:general`
bucket that policy v12's per-storyline budget then treated as a single storyline. Second, *the key does not
depend on the order of the file*: 96 of 1036 pushed cards on the 2026-09-02 day matched two themes at once and
the winner was whichever pattern happened to sit higher, so "Russia helps Iran build missiles" was Middle East
and `\\bstrait\\b` put the Taiwan Strait there too. Matching now produces a *set* of positioned hits and the key
is composed by a fixed rank (asset, conflict, actor, geo, topic), with earliest-mention as the deterministic
tie-break inside a rank. Shuffling the entries cannot change a key.

Aliases are literals, never patterns: `latin` matches on word boundaries and every other script matches as a
substring, both against NFKC-normalized, case-folded text. One alias belongs to exactly one entry (asserted), so
a hit needs no priority rule of its own. Longer aliases are tried first at each position, which is what keeps
`bank of canada` an actor rather than the country and `цб рф` the central bank rather than the state.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import cache
from importlib import resources
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..market_review.instruments import resolve_base_symbol

STORYLINE_REGISTRY_VERSION: Final = "news_storyline_registry_v1"
_REGISTRY_RESOURCE: Final = "storyline_registry.json"

# The key for "this headline names no storyline". It replaces `macro:<dedupe_family>`: the dedupe family is a
# column on the Event row already, and pretending it was a storyline gave policy v12's budget one enormous
# bucket to count. `decide()` exempts exactly this key from the budget (#509 D6).
NO_STORYLINE_KEY: Final = "none"

StorylineKind = Literal["conflict", "actor", "geo", "topic"]
_KIND_RANK: Final[dict[str, int]] = {"conflict": 0, "actor": 1, "geo": 2, "topic": 3}
_SCRIPTS: Final = ("latin", "zh", "ru", "fa", "he")
# Structural regex syntax. Every alias is escaped before it reaches a pattern, so this is not what makes matching
# safe — it is what keeps the registry *data*: a row that tries to smuggle in `.*` is rejected at load.
_REGEX_METACHARACTERS: Final = frozenset(".^$*+?{}[]()|\\")
_ID_SHAPE: Final = re.compile(r"^[a-z0-9_]+$")


class StorylineGate(BaseModel):
    """Gate flags a registry entry carries for `events.gate` (#509 D3). PR-1 stores and validates them only."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    energy_context: bool = False
    macro: bool = False
    queue_high: bool = False


class StorylineAliases(BaseModel):
    """Literal surface forms, per script. `latin` matches on word boundaries; the rest match as substrings."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    latin: tuple[str, ...] = ()
    zh: tuple[str, ...] = ()
    ru: tuple[str, ...] = ()
    fa: tuple[str, ...] = ()
    he: tuple[str, ...] = ()

    def all(self) -> tuple[tuple[str, str], ...]:
        return tuple((script, alias) for script in _SCRIPTS for alias in getattr(self, script))


class StorylineEntry(BaseModel):
    """One storyline the product groups by: a war, an institution, a place, or a subject."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    kind: StorylineKind
    label_zh: str
    aliases: StorylineAliases = StorylineAliases()
    gate: StorylineGate | None = None
    # Conflicts only. `active` is maintained by hand — a war does not expire on a timer (#509 risk 2) — and
    # `members` are the geo/actor entries whose appearance means "this is that war".
    active: bool = False
    members: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _check(self) -> StorylineEntry:
        if not _ID_SHAPE.match(self.id):
            raise ValueError(f"news_storyline_registry_id_invalid:{self.id}")
        if not self.label_zh.strip():
            raise ValueError(f"news_storyline_registry_label_missing:{self.id}")
        if self.kind != "conflict" and (self.active or self.members):
            raise ValueError(f"news_storyline_registry_conflict_fields_on_non_conflict:{self.id}")
        for _script, alias in self.aliases.all():
            if not alias.strip():
                raise ValueError(f"news_storyline_registry_alias_empty:{self.id}")
            if set(alias) & _REGEX_METACHARACTERS:
                raise ValueError(f"news_storyline_registry_alias_not_literal:{self.id}:{alias}")
            if unicodedata.normalize("NFKC", alias).casefold() != alias:
                raise ValueError(f"news_storyline_registry_alias_not_normalized:{self.id}:{alias}")
        return self


class StorylineRegistry(BaseModel):
    """The whole registry. Order is not meaning: nothing here may depend on the order of ``entries``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str
    entries: tuple[StorylineEntry, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _check(self) -> StorylineRegistry:
        if self.version != STORYLINE_REGISTRY_VERSION:
            raise ValueError(f"news_storyline_registry_version_unknown:{self.version}")
        ids = [entry.id for entry in self.entries]
        if len(set(ids)) != len(ids):
            raise ValueError("news_storyline_registry_duplicate_id")
        owner: dict[str, str] = {}
        for entry in self.entries:
            for _script, alias in entry.aliases.all():
                if alias in owner:
                    raise ValueError(f"news_storyline_registry_alias_shared:{alias}:{owner[alias]}:{entry.id}")
                owner[alias] = entry.id
        by_id = {entry.id: entry for entry in self.entries}
        for entry in self.entries:
            for member in entry.members:
                target = by_id.get(member)
                if target is None or target.kind not in {"geo", "actor"}:
                    raise ValueError(f"news_storyline_registry_member_unknown:{entry.id}:{member}")
        return self


@dataclass(frozen=True, slots=True)
class StorylineHit:
    """One alias occurrence: which entry it belongs to, what kind it is, and where it starts."""

    entry_id: str
    kind: str
    start: int
    alias: str


def _registry_document() -> bytes:
    return resources.files("tracefold.news.events").joinpath(_REGISTRY_RESOURCE).read_bytes()


@cache
def load_storyline_registry() -> StorylineRegistry:
    """Load and validate the packaged registry once per process."""

    raw: Any = json.loads(_registry_document().decode("utf-8"))
    return StorylineRegistry.model_validate(raw)


def storyline_entry(entry_id: str) -> StorylineEntry | None:
    """The registry row for one id, or ``None``. The one lookup labels and Gate flags go through."""

    return _entry_index().get(entry_id)


@cache
def _entry_index() -> dict[str, StorylineEntry]:
    return {entry.id: entry for entry in load_storyline_registry().entries}


STORYLINE_REGISTRY_SHA256: Final = hashlib.sha256(_registry_document()).hexdigest()


@cache
def _matchers() -> tuple[tuple[re.Pattern[str], dict[str, StorylineEntry]], ...]:
    """One compiled alternation for the word-bounded Latin aliases and one for every other script.

    Alternatives are ordered longest first so the most specific alias at a position wins and consumes it:
    `bank of canada` beats `canada`, `taiwan strait` beats `taiwan`, `中国人民银行` beats `中国`, `цб рф` beats
    `рф`. The map from matched text back to its entry is exact because an alias belongs to one entry.
    """

    latin: dict[str, StorylineEntry] = {}
    other: dict[str, StorylineEntry] = {}
    for entry in load_storyline_registry().entries:
        for script, alias in entry.aliases.all():
            (latin if script == "latin" else other)[alias] = entry
    out: list[tuple[re.Pattern[str], dict[str, StorylineEntry]]] = []
    for table, template in ((latin, r"(?<![a-z0-9])(?:{})(?![a-z0-9])"), (other, r"(?:{})")):
        if not table:
            continue
        alternation = "|".join(re.escape(alias) for alias in sorted(table, key=lambda a: (-len(a), a)))
        out.append((re.compile(template.format(alternation)), table))
    return tuple(out)


def normalize_storyline_text(text: str) -> str:
    """NFKC then case-fold: full-width digits, compatibility forms and every script's case are one surface."""

    return unicodedata.normalize("NFKC", text).casefold()


def match_storyline(text: str) -> tuple[StorylineHit, ...]:
    """Every registry alias occurrence in ``text``, earliest first. Order of the registry does not reach here."""

    folded = normalize_storyline_text(text)
    hits: list[StorylineHit] = []
    for pattern, table in _matchers():
        for found in pattern.finditer(folded):
            entry = table[found.group(0)]
            hits.append(StorylineHit(entry_id=entry.id, kind=entry.kind, start=found.start(), alias=found.group(0)))
    return tuple(sorted(hits, key=lambda hit: (hit.start, _KIND_RANK[hit.kind], hit.entry_id)))


def registry_storyline_key(text: str) -> str | None:
    """The registry's key for a text, or ``None`` when nothing matched.

    The rank is fixed and the tie-break inside a rank is the earliest mention, so neither the file's order nor
    the number of entries can move a key. A conflict wins over its own participants: on a war day the product
    wants one line for the war, not one per country (#509 D2).
    """

    hits = match_storyline(text)
    if not hits:
        return None
    first_seen: dict[str, int] = {}
    for hit in hits:
        first_seen.setdefault(hit.entry_id, hit.start)
    conflicts: list[tuple[int, str]] = []
    for entry in load_storyline_registry().entries:
        if entry.kind != "conflict" or not entry.active:
            continue
        positions = [first_seen[name] for name in (entry.id, *entry.members) if name in first_seen]
        if positions:
            conflicts.append((min(positions), entry.id))
    if conflicts:
        return f"conflict:{min(conflicts)[1]}"
    for kind in ("actor", "geo", "topic"):
        ranked = sorted((hit.start, hit.entry_id) for hit in hits if hit.kind == kind)
        if ranked:
            return f"{kind}:{ranked[0][1]}"
    return None


_CL_SYMBOLS: Final = frozenset({"CL", "XYZ-CL"})


# A model primary is free text (`TriageAsset.symbol` is any 1-16 characters) and this fallback is reached
# precisely when nothing grounded it, so it is the least validated string in the pipeline — and it becomes a
# duplicate-comparison group, an advisory-lock key and a console label. Accept only something shaped like a
# symbol. #509 widened it by one optional exchange suffix: `02015.HK` and `DTE.DE` are exactly as groupable as
# `NVDA`, and rejecting them sent every Hong Kong and German single-name card to the fallback bucket instead.
_SYMBOL_SHAPE: Final = re.compile(r"^[A-Z0-9]{1,10}(\.[A-Z]{1,4})?$")


def _symbol_in_text(symbol: str, text: str) -> bool:
    """True when the base symbol appears in the text as its own uppercase token (a `$TICKER` cashtag counts: `$`
    is not a word character).

    Case-sensitive on purpose. Provider tags collide with ordinary English words — `NOT`, `ME`, `ID`, `IO`, `ON`,
    `AI` are all real symbols — and a case-insensitive match turned "he will not sell his stake" into evidence for
    `asset:NOT`, which is the exact mis-bucketing this fallback exists to prevent. A Chinese headline carrying the
    ticker still matches, because it carries it in caps. Strict on token boundaries too: `ETH` does not match
    `ETHEREUM`, `MU` does not match `MUSK`."""

    base = re.escape(symbol.replace("XYZ-", "").upper())
    return re.search(rf"(?<![A-Za-z0-9]){base}(?![A-Za-z0-9])", text) is not None


def _asset_key(symbols: Sequence[str], aliases: Mapping[str, str] | None) -> str:
    """``asset:<SYM>`` for the first symbol after alias resolution (#75 collapses one issuer's contracts)."""

    return f"asset:{sorted(resolve_base_symbol(symbol, aliases) for symbol in symbols)[0]}"


def preliminary_storyline_key(
    *, title: str, grounded_assets: Sequence[str], asset_class: str, dedupe_family: str
) -> str:
    """Key computed before Triage (status bar only), on the same rank as the final key.

    There is no verdict yet, so the Gate's grounded tags stand in for the model's primaries; the final key then
    follows the verdict. ``dedupe_family`` is accepted and unused: it is a column on the Event row, and #509
    stopped pretending it was a storyline."""

    scope = "macro" if asset_class in {"macro", "none"} else "single_name"
    return final_storyline_key(
        title=title,
        headline_zh="",
        scope=scope,
        verdict_primaries=grounded_assets,
        grounded_assets=grounded_assets,
        dedupe_family=dedupe_family,
    )


def final_storyline_key(
    *,
    title: str,
    headline_zh: str,
    scope: str,
    verdict_primaries: Sequence[str],
    grounded_assets: Sequence[str],
    dedupe_family: str,
    aliases: Mapping[str, str] | None = None,
    degraded: bool = False,
) -> str:
    """Key computed after Triage, by the fixed #509 rank:

    1. ``asset:<SYM>`` — a verdict primary the Gate grounded, when the scope is not macro;
    2. ``conflict:<id>`` — an active conflict whose own aliases or members the text names;
    3. ``actor:<id>``, 4. ``geo:<id>``, 5. ``topic:<id>`` — earliest mention inside the kind;
    6. the model's own symbol-shaped primary, even when the provider did not tag it (#100);
    7. a grounded tag the text actually names (#100);
    8. ``none`` — no storyline.

    ``aliases`` resolves symbols to one issuer first (#75). ``dedupe_family`` is accepted and unused: the
    fallback key is now ``none``, so the family stays a column instead of becoming a budget bucket (#509 D2).

    ``degraded`` marks a rule-baseline verdict, whose ``assets`` are empty by construction (see
    ``triage_rules.fallback_verdict``). "The model named no primary" is evidence only when a model actually
    answered, so a degraded card keeps the pre-#100 fallback: the provider's tags are the only evidence there is.

    This key is a duplicate-comparison and operator-facing grouping, never a claim shown to the reader — the
    card's tickers come from ``delivery.card_assets`` (verdict primaries ∩ grounded), which this does not touch."""

    grounded = {resolve_base_symbol(a, aliases) for a in grounded_assets}
    primaries = [
        a for a in verdict_primaries if a.upper() not in _CL_SYMBOLS and resolve_base_symbol(a, aliases) in grounded
    ]
    if primaries and scope != "macro":
        return _asset_key(primaries, aliases)
    text = f"{title} {headline_zh}"
    key = registry_storyline_key(text)
    if key is not None:
        return key
    # Nothing in the registry matched. The model named the subject even when the provider did not tag it, and its
    # own primary is a better bucket than an arbitrary grounded tag: OKX's listing notices all carry an `OKB` tag,
    # so "Johnson & Johnson appears on OKX" was keyed `asset:OKB`; VeChain's upgrade vote was keyed `asset:SKHY`.
    # 16% of the asset-keyed cards of a live day sat in a bucket that was not about them (#100).
    named = [
        a
        for a in verdict_primaries
        if a.upper() not in _CL_SYMBOLS and _SYMBOL_SHAPE.match(a.upper().replace("XYZ-", ""))
    ]
    if named:
        return _asset_key(named, aliases)
    # A model that answered and still named nothing is saying the headline has no tradable subject, so a provider
    # tag is only a storyline when the text is actually about it — the symbol appearing as its own token is the
    # cheap evidence for that. Everything else has no storyline at all: `asset:BTC` was collecting Polish jets
    # scrambling and a lending protocol being drained, which polluted duplicate evidence for real BTC cards. A
    # false negative here costs a coarser group; a false positive contaminates another card's comparison set. A
    # degraded verdict is exempt: it has no `assets` to begin with, and "NVIDIA to invest $100bn" never spells
    # `NVDA`.
    fallback = [a for a in grounded_assets if a.upper() not in _CL_SYMBOLS and (degraded or _symbol_in_text(a, text))]
    if fallback:
        return _asset_key(fallback, aliases)
    return NO_STORYLINE_KEY


__all__ = [
    "NO_STORYLINE_KEY",
    "STORYLINE_REGISTRY_SHA256",
    "STORYLINE_REGISTRY_VERSION",
    "StorylineAliases",
    "StorylineEntry",
    "StorylineGate",
    "StorylineHit",
    "StorylineRegistry",
    "final_storyline_key",
    "load_storyline_registry",
    "match_storyline",
    "normalize_storyline_text",
    "preliminary_storyline_key",
    "registry_storyline_key",
    "storyline_entry",
]
