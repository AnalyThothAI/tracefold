"""Framework-neutral public Interface for News semantic judgment.

Callers construct one immutable :class:`TriageContext` and invoke one
``SemanticJudge.judge`` method.  DSPy, Predictor state, Program artifacts,
model routes and compiler state are deliberately absent from this module.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final, Literal, Protocol, cast, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..artifact_identity import canonical_sha
from ..evidence import PreparedEvidence, VisibleEvidenceSpan, assemble_evidence, query_for
from ..models import (
    FACT_KINDS,
    FactKind,
    MarketAsset,
    MarketType,
    TriageAsset,
    TriageVerdict,
    base_symbol,
    market_type_of,
)
from ..taxonomy import NewsTaxonomyV1, SourceAuthority
from ..told_context import TOLD_MAX as _TOLD_MAX
from ..told_context import TOLD_SYMBOLS_MAX as _TOLD_SYMBOLS_MAX
from ..told_context import ToldLedgerSnapshot as _ToldLedgerSnapshot

# 16, not 12. Measured on every accepted restatement whose duplicate target was inside the 4 h ledger (n=22),
# target recall@N: 12 rows recalls 19, 16 rows recalls 21. The binding constraint on the old selector was never
# the ranking — a dense storyline puts 18-22 genuinely related cards in one window — so no ordering recovers
# what the cap excludes. ReaderCard no longer receives the ledger at all, which more than pays for the four
# extra rows: the two-call total moves ~+2%.
# No tier may take every slot. Ranking storyline first and filling the rest by tier order scored *below* the
# predecessor (18/22 against 19/22): with 14-17 same-storyline cards in the window, tier 1 consumed all 16 rows
# and the shared-instrument evidence that actually held the duplicate never got one. Capping the storyline tier
# is what makes the lower tiers reachable.
# Retrieval's own threshold on comparison titles, deliberately not `news.policy.similarity_max`: that knob is
# operator-owned duplicate policy over reader headlines, and coupling the two would let a policy edit silently
# change what the model is allowed to see.
# What a told entry carried before #675 §1 and does not carry now. A ledger entry projects the verdict,
# so an archived context holds whatever the verdict held on the day it was recorded.
_RETIRED_TOLD_KEYS: Final[frozenset[str]] = frozenset({"magnitude"})
WATCHLIST_MAX: Final[int] = 64
GROUNDED_ASSETS_MAX: Final[int] = 16
# What the catalogue can say about the symbols this Event already carries, bounded (#651 §A). Eight
# symbols because `GROUNDED_ASSETS_MAX` is the wider evidence list and the candidates are only worth
# showing for the ones the model is actually choosing between; four classes because the vocabulary has
# seven and a symbol the catalogue holds under more than four is not a disambiguation the prompt can help
# with. Bounds, not budgets: an over-long list is truncated, never an error.
CATALOG_CANDIDATE_SYMBOLS_MAX: Final[int] = 8
CATALOG_CANDIDATE_CLASSES_MAX: Final[int] = 4
STRATEGIES_MAX: Final[int] = 16

EDITORIAL_CONTRACT_VERSION: Final[Literal["news_editorial_v4"]] = "news_editorial_v4"
JUDGMENT_CONTRACT_VERSION: Final[Literal["news_judgment_v3"]] = "news_judgment_v3"


class _ExactContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ReaderCardSemanticView(_ExactContractModel):
    """The complete semantic Interface visible to ``ReaderCard``.

    It intentionally excludes every judgment about the reader.  ReaderCard writes factual copy; it does
    not get a second opportunity to infer urgency or final action.  ``fact_kind`` is here because it says
    what the card is *about* -- a level crossed, a measure taken, a figure restated -- which is copy
    guidance, and it is the only one of the old seven relevance codes that survived #675 §1.
    """

    assets: tuple[TriageAsset, ...] = Field(default=(), max_length=8)
    direction: Literal["bullish", "bearish", "neutral", "unclear"]
    fact_kind: FactKind
    novelty: Literal["new_fact", "progression", "restatement"]
    restates: int = Field(default=-1, ge=-1)
    scope: Literal["macro", "sector", "single_name"]


class EditorialEnvelope(_ExactContractModel):
    """The one current editorial sibling persisted atomically with a verdict.

    v3 (#651 §5.3) separated the two things v2 kept in one required object. ``source_authority`` is a
    code fact: `source_authority_from_evidence` reads it off the frozen evidence, the model never emits
    it, and it is therefore present on every model judgment whatever the taxonomy Predictor did.
    ``taxonomy`` is the taxonomy Predictor's answer, and a Predictor can fail on its own -- a truncated
    completion, a provider refusal, a typed rejection -- without costing the reader the card the other
    two Predictors produced. ``taxonomy_status`` names which of those two happened and
    ``taxonomy_error_code`` carries the `news_program_*` code when it is the second.

    v4 (#675 §1) drops ``relevance``. The seven `TradeRelevanceV1` codes were the model's own answer to
    "should the reader be woken", and the envelope is the place a *code* fact about the evidence is
    persisted beside the verdict -- which is what the two survivors are. What the model observes about
    the text now lives on the verdict (`fact_kind`), and what the reader gets is decided from these
    facts by `triage_rules.decide()`.

    The uncorroborated-escalate rule (`triage_rules.decide`) is why the taxonomy split is not cosmetic:
    under v2 the rule read `taxonomy.source_authority`, so a taxonomy failure would have taken the
    corroboration evidence down with the label, and the loudest card class would have lost its safety
    rule to an unrelated model failure.
    """

    editorial_contract_version: Literal["news_editorial_v4"] = EDITORIAL_CONTRACT_VERSION
    editorial_origin: Literal["model"] = "model"
    source_authority: SourceAuthority
    taxonomy: NewsTaxonomyV1 | None = None
    taxonomy_status: Literal["available", "unavailable"] = "available"
    taxonomy_error_code: str | None = None
    editorial_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="before")
    @classmethod
    def _adapt_pre_cut_document(cls, value: Any) -> Any:
        """Read a stored `news_editorial_v3` or `_v2` document into the v4 shape (#675 §1).

        The ledger holds three editorial contracts and rewrites none of them, so every learning surface
        that validates a stored envelope -- the release metric, the frozen corpus, the observed-episode
        projection -- would otherwise raise on the first row written before this cut. That is not a
        hypothetical: `storage.learning` selects `news_judgment_v2` rows by name so the corpus keeps its
        history, and a 30-day retention means most of it is still pre-cut.

        The stored hash is *verified before anything is dropped*, which is the whole point of doing this
        here rather than at each call site: the document is checked against the digest the writer computed
        over it, and only then is `relevance` -- the seven `TradeRelevanceV1` codes #675 §1 deleted --
        removed and the v4 digest computed over what is left. A row whose hash does not address its own
        content is a corrupted row and still raises. v2 additionally nests the authority inside the
        taxonomy, which #651 §5.3 lifted out; the same lift happens here and is the same lift
        `storage.decisions.editorial_read_shape` performs for the API.
        """

        if not isinstance(value, Mapping):
            return value
        version = str(value.get("editorial_contract_version") or "")
        if version in {"", EDITORIAL_CONTRACT_VERSION}:
            return value
        if version not in {"news_editorial_v3", "news_editorial_v2"}:
            raise ValueError("news_editorial_contract_version_unknown")
        stored = {key: item for key, item in value.items() if key != "editorial_sha256"}
        if str(value.get("editorial_sha256") or "") != canonical_sha(stored):
            raise ValueError("news_editorial_hash_mismatch")
        taxonomy = value.get("taxonomy")
        if version == "news_editorial_v2":
            if not isinstance(taxonomy, Mapping):
                raise ValueError("news_editorial_taxonomy_status_invalid")
            authority = str(taxonomy.get("source_authority") or "unknown")
            # A v2 row exists only because its taxonomy validated: the whole judgment failed otherwise.
            axes: Any = {key: item for key, item in taxonomy.items() if key != "source_authority"}
            status, error_code = "available", None
        else:
            authority = str(value.get("source_authority") or "unknown")
            axes = dict(taxonomy) if isinstance(taxonomy, Mapping) else None
            status = str(value.get("taxonomy_status") or "available")
            error_code = value.get("taxonomy_error_code") or None
        payload = {
            "editorial_contract_version": EDITORIAL_CONTRACT_VERSION,
            "editorial_origin": "model",
            "source_authority": authority,
            "taxonomy": axes,
            "taxonomy_status": status,
            "taxonomy_error_code": error_code,
        }
        return {**payload, "editorial_sha256": canonical_sha(payload)}

    @classmethod
    def issue(
        cls,
        *,
        source_authority: SourceAuthority,
        taxonomy: NewsTaxonomyV1 | None = None,
        taxonomy_error_code: str | None = None,
    ) -> EditorialEnvelope:
        payload = {
            "editorial_contract_version": EDITORIAL_CONTRACT_VERSION,
            "editorial_origin": "model",
            "source_authority": source_authority,
            "taxonomy": None if taxonomy is None else taxonomy.model_dump(mode="json"),
            "taxonomy_status": "available" if taxonomy is not None else "unavailable",
            "taxonomy_error_code": None if taxonomy is not None else taxonomy_error_code,
        }
        return cls(**payload, editorial_sha256=canonical_sha(payload))

    @model_validator(mode="after")
    def _origin_and_identity_are_exact(self) -> EditorialEnvelope:
        available = self.taxonomy_status == "available"
        if available != (self.taxonomy is not None) or available != (self.taxonomy_error_code is None):
            raise ValueError("news_editorial_taxonomy_status_invalid")
        if not available and not str(self.taxonomy_error_code or "").startswith("news_program_"):
            raise ValueError("news_editorial_taxonomy_error_code_invalid")
        payload = self.model_dump(mode="json", exclude={"editorial_sha256"})
        if self.editorial_sha256 != canonical_sha(payload):
            raise ValueError("news_editorial_hash_mismatch")
        return self


class FrozenEventEvidence(_ExactContractModel):
    """Immutable evidence identity plus the bounded evidence visible to the Program."""

    event_id: str
    evidence_version: int = Field(ge=0)
    evidence_sha256: str
    focus_fact_id: str
    source: str = ""
    strategies: tuple[str, ...] = Field(default=(), max_length=STRATEGIES_MAX)
    engine_type: str = "unknown"
    title: str = Field(max_length=600)
    raw_first_line: str = Field(default="", max_length=300)
    content: str = Field(default="", max_length=600)
    published_at_ms: int = Field(ge=0)
    member_count: int = Field(default=1, ge=1)
    dedupe_family: str = "general"
    provider_score: int | None = None
    provider_coins: tuple[str, ...] = Field(default=(), max_length=10)
    queue_priority: Literal["high", "normal"] = "normal"
    # Selector input only, never rendered: the Deduper's normalized title, which is what a same-fact
    # comparison against a prior Event is actually made of.  The model reads `title`.
    comparison_title: str = Field(default="", max_length=600)


class CatalogCandidate(_ExactContractModel):
    """What the instrument catalogue holds for one symbol this Event already names.

    Code evidence, not an answer. `instrument_classes()` collapses a symbol to one class so the Gate can
    ask "coin or stock"; that collapse is what makes `SEI` look unambiguous when the catalogue in fact
    carries a Binance token *and* a NYSE ticker under it. The uncollapsed list is what lets the model see
    the ambiguity, and what lets code tell an asset it can prove unambiguous from one it cannot.
    """

    symbol: str = Field(min_length=1, max_length=16)
    classes: tuple[MarketType, ...] = Field(default=(), max_length=CATALOG_CANDIDATE_CLASSES_MAX)


def catalog_candidates_of(
    candidates: Mapping[str, Sequence[str]] | None,
    symbols: Sequence[str],
) -> tuple[CatalogCandidate, ...]:
    """The bounded candidate rows for the symbols one Event carries, in that Event's own symbol order."""

    if not candidates:
        return ()
    rows: list[CatalogCandidate] = []
    seen: set[str] = set()
    for value in symbols:
        symbol = base_symbol(str(value))
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        classes = candidates.get(symbol)
        if not classes:
            continue
        rows.append(
            CatalogCandidate(
                symbol=symbol,
                classes=tuple(dict.fromkeys(market_type_of(item) for item in classes))[:CATALOG_CANDIDATE_CLASSES_MAX],
            )
        )
        if len(rows) >= CATALOG_CANDIDATE_SYMBOLS_MAX:
            break
    return tuple(rows)


def unambiguous_catalog_class(
    candidates: Sequence[CatalogCandidate],
    symbol: str,
) -> MarketType:
    """The one market the catalogue proves for a symbol, or ``unknown`` when it holds none or several."""

    base = base_symbol(str(symbol))
    for candidate in candidates:
        if candidate.symbol == base:
            return candidate.classes[0] if len(candidate.classes) == 1 else "unknown"
    return "unknown"


class SemanticGateContext(_ExactContractModel):
    asset_class: str = "none"
    grounded_assets: tuple[str, ...] = Field(default=(), max_length=GROUNDED_ASSETS_MAX)
    # #651 §A: what the catalogue holds for each symbol already grounded on this Event, uncollapsed. The
    # Gate's own collapsed `asset_class` stays exactly as it was; this is beside it, not instead of it.
    catalog_candidates: tuple[CatalogCandidate, ...] = Field(default=(), max_length=CATALOG_CANDIDATE_SYMBOLS_MAX)
    macro_lexicon: bool = False
    pr_template: bool = False


class _ModelVisibleEvent(_ExactContractModel):
    source: str
    strategies: tuple[str, ...] = Field(max_length=STRATEGIES_MAX)
    engine_type: str
    title: str = Field(max_length=600)
    published_at_ms: int = Field(ge=0)
    member_count: int = Field(ge=1)
    dedupe_family: str
    provider_coins: tuple[str, ...] = Field(max_length=10)


class _ModelVisibleGate(_ExactContractModel):
    asset_class: str
    grounded_assets: tuple[str, ...] = Field(max_length=GROUNDED_ASSETS_MAX)
    catalog_candidates: tuple[CatalogCandidate, ...] = Field(max_length=CATALOG_CANDIDATE_SYMBOLS_MAX)
    pr_template: bool


class _ModelVisibleToldEntry(_ExactContractModel):
    provenance_status: str
    i: int = Field(ge=0)
    ago_min: int = Field(ge=0)
    storyline_key: str
    comparison_title: str = Field(max_length=600)
    symbols: tuple[str, ...] = Field(max_length=_TOLD_SYMBOLS_MAX)
    assets: tuple[MarketAsset, ...] = Field(max_length=_TOLD_SYMBOLS_MAX)
    direction: str
    headline_zh: str = Field(max_length=60)
    why_zh: str = Field(max_length=140)


class _ModelVisibleEventStatus(_ExactContractModel):
    storyline_key: str
    preliminary: bool
    told: tuple[_ModelVisibleToldEntry, ...] = Field(max_length=_TOLD_MAX)


class ModelVisibleSemanticsInput(_ExactContractModel):
    """Exact bounded JSON shape visible to ``EventSemantics``: current evidence plus the selected ledger."""

    current_evidence: tuple[VisibleEvidenceSpan, ...] = Field(max_length=12)
    related_evidence: tuple[VisibleEvidenceSpan, ...] = Field(max_length=24)
    event: _ModelVisibleEvent
    gate: _ModelVisibleGate
    event_status: _ModelVisibleEventStatus


class ModelVisibleCardInput(_ExactContractModel):
    """Exact bounded JSON shape visible to ``ReaderCard``.

    There is no ``event_status`` field and no place to put one: novelty is ``EventSemantics``' job, and a copy
    step that can re-read old cards can re-interpret them.  The boundary is the schema, not a prompt reminder.
    """

    current_evidence: tuple[VisibleEvidenceSpan, ...] = Field(max_length=12)
    related_evidence: tuple[VisibleEvidenceSpan, ...] = Field(max_length=24)
    event: _ModelVisibleEvent
    gate: _ModelVisibleGate


class ModelVisibleTaxonomyInput(_ExactContractModel):
    """Exact bounded JSON shape visible to the taxonomy Predictor: evidence and Gate facts, no told ledger.

    Taxonomy answers "what happened" from the Event alone (#501 D1). Reader history is novelty evidence,
    and a classifier that could read it could be taught to label by what was already sent.
    """

    current_evidence: tuple[VisibleEvidenceSpan, ...] = Field(max_length=12)
    event: _ModelVisibleEvent
    gate: _ModelVisibleGate


class TriageContext(_ExactContractModel):
    """One immutable question at the semantic-judgment Seam."""

    evidence: FrozenEventEvidence
    prepared_evidence: PreparedEvidence | None = None
    gate: SemanticGateContext
    watchlist: tuple[str, ...] = Field(default=(), max_length=WATCHLIST_MAX)
    told: _ToldLedgerSnapshot
    now_ms: int = Field(ge=0)
    queue_lag_ms: int = Field(default=0, ge=0)

    @classmethod
    def from_card(
        cls,
        card: Mapping[str, Any],
        *,
        watchlist: Sequence[str],
        told_rows: Sequence[Mapping[str, Any]],
        now_ms: int,
        queue_lag_ms: int,
        catalog_candidates: Mapping[str, Sequence[str]] | None = None,
        prepared_evidence: PreparedEvidence | None = None,
    ) -> TriageContext:
        """One immutable question, including what the catalogue currently holds for this Event's symbols.

        ``catalog_candidates`` is resolved at judgment time rather than frozen into the Event's immutable
        evidence: the catalogue is a living snapshot of what venues list today, and freezing a stale copy
        of it into evidence would make the model's disambiguation evidence age with the Event instead of
        with the universe it describes. It is bounded, code-owned, and reproducible for one catalogue.
        """

        metadata = dict(card.get("provider_metadata") or {})
        coins = tuple(
            f"{coin.get('symbol')}:{coin.get('grade') or '-'}"
            for coin in metadata.get("coins") or ()
            if isinstance(coin, Mapping) and coin.get("symbol")
        )[:10]
        storyline_key = str(card.get("storyline_key") or "")
        candidates = catalog_candidates_of(
            catalog_candidates, tuple(str(value) for value in card.get("grounded_assets") or ())
        )
        prepared = prepared_evidence or assemble_evidence(
            card, {}, query=query_for(card, {}, cutoff=now_ms), candidates=()
        )
        return cls(
            prepared_evidence=prepared,
            evidence=FrozenEventEvidence(
                event_id=str(card.get("event_id") or ""),
                evidence_version=int(card.get("evidence_version") or 0),
                evidence_sha256=str(card.get("evidence_sha256") or ""),
                focus_fact_id=str(card.get("focus_fact_id") or ""),
                source=str(card.get("reporting_origin") or ""),
                strategies=tuple(str(value) for value in card.get("provenance") or ())[:STRATEGIES_MAX],
                engine_type=str(card.get("engine_type") or "unknown"),
                title=str(card.get("leader_title") or "")[:600],
                raw_first_line=str(card.get("raw_first_line") or "")[:300],
                content=str(card.get("leader_description") or "")[:600],
                published_at_ms=int(card.get("opened_at_ms") or card.get("published_at_ms") or 0),
                member_count=max(1, int(card.get("member_count") or 1)),
                dedupe_family=str(card.get("dedupe_family") or "general"),
                provider_score=card.get("provider_score_max"),
                provider_coins=coins,
                queue_priority=str(card.get("queue_priority") or "normal"),
                comparison_title=str(card.get("comparison_title") or "")[:600],
            ),
            gate=SemanticGateContext(
                asset_class=str(card.get("asset_class") or "none"),
                grounded_assets=tuple(str(value) for value in card.get("grounded_assets") or ())[:GROUNDED_ASSETS_MAX],
                catalog_candidates=catalog_candidates_of(
                    catalog_candidates,
                    [
                        *(str(value) for value in card.get("grounded_assets") or ()),
                        *(
                            str(coin.get("symbol"))
                            for coin in metadata.get("coins") or ()
                            if isinstance(coin, Mapping) and coin.get("symbol")
                        ),
                    ],
                ),
                macro_lexicon=bool(card.get("macro_lexicon")),
                pr_template=bool(card.get("pr_template"))
                or str(card.get("admission") or "").startswith("suppressed_pr"),
            ),
            watchlist=tuple(str(value) for value in watchlist)[:WATCHLIST_MAX],
            told=_ToldLedgerSnapshot.select(
                told_rows,
                now_ms=now_ms,
                storyline_key=storyline_key,
                symbols=tuple(
                    MarketAsset(str(value), unambiguous_catalog_class(candidates, str(value)))
                    for value in card.get("grounded_assets") or ()
                ),
                comparison_title=str(card.get("comparison_title") or ""),
                exclude_event_id=str(card.get("event_id") or ""),
            ),
            now_ms=int(now_ms),
            queue_lag_ms=max(0, int(queue_lag_ms)),
        )

    @classmethod
    def adapt_archived(cls, document: Mapping[str, Any]) -> tuple[dict[str, Any], str | None]:
        """One archived context in the shapes this contract still validates, and what wrote it.

        Two adaptations, both explicit, both named here rather than repeated at each read boundary.

        The first is `news_evidence_input_v1`: a prepared-evidence block from before #651's input version
        is dropped rather than replayed, because reanalysing it would be today's assembly answering an old
        question. That one is older than this cut and only moved here.

        The second is #675 §1. A told entry projects the verdict it was written from, so every entry
        archived before this contract carries the deleted ``magnitude`` -- and `ToldLedgerEntry` and
        `_ModelVisibleToldEntry` both forbid unknown keys, so a recorded `TriageContext` from last week
        fails validation outright. `dataset._selected_context` answered that failure with ``None`` and the
        episode silently left the corpus; the learning plane would have quietly lost its entire pre-cut
        history to a field nobody reads. The key is stripped and the contract that wrote it is returned,
        so the caller records a reconstruction instead of inventing an exact replay (#679 review 2).

        Returns the adapted document and the contract version it was written under, or ``None`` when the
        document already matches the current one and nothing was changed.
        """

        archived = dict(document)
        if dict(archived.get("prepared_evidence") or {}).get("input_version") == "news_evidence_input_v1":
            archived["prepared_evidence"] = None
        told = archived.get("told")
        if not isinstance(told, Mapping):
            return archived, None
        entries = told.get("entries")
        if not isinstance(entries, Sequence) or isinstance(entries, str | bytes):
            return archived, None
        if not any(isinstance(entry, Mapping) and _RETIRED_TOLD_KEYS & set(entry) for entry in entries):
            return archived, None
        archived["told"] = {
            **told,
            "entries": [
                {key: item for key, item in entry.items() if key not in _RETIRED_TOLD_KEYS}
                if isinstance(entry, Mapping)
                else entry
                for entry in entries
            ],
        }
        return archived, "news_judgment_v2"

    def adapt_archived_excerpt(self) -> TriageContext:
        """Explicit input-study conversion using only archived previews, never today's database.

        This answers a new v12 question and must not be called exact historical replay.
        Existing serialized executions and accepted labels are never rewritten.
        """
        if self.prepared_evidence is not None:
            return self
        card = {
            "event_id": self.evidence.event_id,
            "focus_fact_id": self.evidence.focus_fact_id,
            "leader_title": self.evidence.title,
            "raw_first_line": self.evidence.raw_first_line,
            "leader_description": self.evidence.content,
        }
        prepared = assemble_evidence(card, {}, query=query_for(card, {}, cutoff=self.now_ms), candidates=())
        return self.model_copy(update={"prepared_evidence": prepared})

    def _visible_evidence(self, kind: str) -> tuple[VisibleEvidenceSpan, ...]:
        if self.prepared_evidence is None:
            # Historical archives explicitly lack the prepared input. No today's material lookup.
            return ()
        spans = (
            self.prepared_evidence.current_evidence if kind == "current" else self.prepared_evidence.related_evidence
        )
        return tuple(span.visible() for span in spans)

    def _visible_event(self) -> _ModelVisibleEvent:
        event = self.evidence
        return _ModelVisibleEvent(
            source=event.source,
            strategies=event.strategies,
            engine_type=event.engine_type,
            title=event.title,
            published_at_ms=event.published_at_ms,
            member_count=event.member_count,
            dedupe_family=event.dedupe_family,
            provider_coins=event.provider_coins,
        )

    def _visible_gate(self) -> _ModelVisibleGate:
        return _ModelVisibleGate(
            asset_class=self.gate.asset_class,
            grounded_assets=self.gate.grounded_assets,
            catalog_candidates=self.gate.catalog_candidates,
            pr_template=self.gate.pr_template,
        )

    def event_semantics_payload(self) -> dict[str, Any]:
        """Bounded evidence plus the selected told context, with audit-only ids removed."""

        return ModelVisibleSemanticsInput(
            event=self._visible_event(),
            current_evidence=self._visible_evidence("current"),
            related_evidence=self._visible_evidence("related"),
            gate=self._visible_gate(),
            event_status=_ModelVisibleEventStatus(
                storyline_key=self.told.storyline_key,
                preliminary=self.told.preliminary,
                told=tuple(
                    _ModelVisibleToldEntry(
                        i=entry.i,
                        provenance_status=entry.provenance_status,
                        ago_min=entry.ago_min,
                        storyline_key=entry.storyline_key,
                        comparison_title=entry.comparison_title,
                        symbols=entry.symbols,
                        assets=entry.assets,
                        direction=entry.direction,
                        headline_zh=entry.headline_zh,
                        why_zh=entry.why_zh,
                    )
                    for entry in self.told.entries
                ),
            ),
        ).model_dump(mode="json")

    def taxonomy_payload(self) -> dict[str, Any]:
        """Bounded evidence only. Taxonomy classifies what this Event says, never what was already told."""

        return ModelVisibleTaxonomyInput(
            event=self._visible_event(), gate=self._visible_gate(), current_evidence=self._visible_evidence("current")
        ).model_dump(mode="json")

    def reader_card_payload(self) -> dict[str, Any]:
        """Bounded evidence only. The card is written from what this Event says, not from what was told."""

        return ModelVisibleCardInput(
            event=self._visible_event(),
            gate=self._visible_gate(),
            current_evidence=self._visible_evidence("current"),
            related_evidence=self._visible_evidence("related"),
        ).model_dump(mode="json")

    def selected_context_sha256(self) -> str:
        """Identity of exactly what the model was shown. Audit and replay identity."""

        return canonical_sha(self.event_semantics_payload()["event_status"]["told"])

    def novelty_context_sha256(self) -> str:
        """Identity of the shown rows that are *evidence about this candidate*, which is what a re-ask is for.

        The recency tier is filler: it is there so a sparse candidate still sees what the reader has been
        reading, and a card at the top of it cannot turn this Event into a restatement of anything. Hashing it
        too would put the whole selection back under "any delivery invalidates the judgment", which is the rule
        this replaced. A card that joins on storyline, instrument or same-fact similarity does change the
        question, and does earn the second execution.
        """

        return canonical_sha(
            [
                {
                    "storyline_key": entry.storyline_key,
                    "comparison_title": entry.comparison_title,
                    "comparison_fingerprint": entry.comparison_fingerprint,
                    "symbols": list(entry.symbols),
                    "assets": [{"symbol": a.symbol, "market_type": a.market_type} for a in entry.assets],
                    "direction": entry.direction,
                    "headline_zh": entry.headline_zh,
                    "why_zh": entry.why_zh,
                    "tier": entry.tier,
                }
                for entry in self.told.entries
                if entry.tier != "recency"
            ]
        )


class ProgramNormalizationTrace(_ExactContractModel):
    # v3 (#675 §1): the only normalization left is the restatement index. `channels` and
    # `affected_markets` were bounded code sets whose emission order the model could not control, so the
    # Program canonicalized them and recorded the rewrite; both fields are gone.
    normalizer_id: Literal["semantic_normalizer_v3"] = "semantic_normalizer_v3"
    field: Literal["restates"]
    reason: Literal["non_restatement_index_ignored"]
    input_value: int
    output_value: int

    @model_validator(mode="after")
    def _field_and_values_match_reason(self) -> ProgramNormalizationTrace:
        if self.field == "restates":
            if (
                self.reason != "non_restatement_index_ignored"
                or not isinstance(self.input_value, int)
                or self.input_value < 0
                or self.output_value != -1
            ):
                raise ValueError("news_program_restatement_normalization_invalid")
            return self
        if (
            self.reason != "canonical_set_order"
            or not isinstance(self.input_value, tuple)
            or not isinstance(self.output_value, tuple)
        ):
            raise ValueError("news_program_relevance_normalization_invalid")
        return self


class ProgramCallTrace(_ExactContractModel):
    predictor: Literal["event_semantics", "taxonomy", "reader_card"]
    route: Literal["primary", "fallback"]
    attempt: int
    request_sha256: str
    input_sha256: str
    model_binding: str
    physical_provider_call: bool = False
    runtime_provider: str | None = None
    runtime_model: str | None = None
    runtime_model_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    runtime_binding_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    upstream_sha256: str | None = None
    output_sha256: str | None = None
    validated_output: dict[str, Any] | None = None
    normalizations: tuple[ProgramNormalizationTrace, ...] = Field(default=(), max_length=3)
    provider: str | None = None
    model: str | None = None
    model_sha256: str | None = None
    latency_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    total_tokens: int = 0
    # Older zero-filled traces have unknown coverage. New calls distinguish a
    # provider-reported zero from counters the provider never returned.
    usage_coverage: Literal["complete", "partial", "unknown"] = "unknown"
    provider_cost_microusd: int | None = None
    finish_reason: str | None = None
    error_code: str | None = None
    # Bounded, secret-scrubbed provider error body for a refused request (#310). Absent on every
    # trace written before that epoch, and on any attempt the provider answered.
    error_detail: str | None = None
    terminal_disposition: (
        Literal[
            "provider_success",
            "provider_error",
            "adapter_parse_error",
            "domain_validation_error",
            "timeout_cancelled",
            "late_completion",
        ]
        | None
    ) = None
    # The actual request address intentionally excludes logical route metadata;
    # this second address binds the physical request to its Program invocation.
    invocation_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    # CandidateEvaluator may persist exact record/replay material, but verdict
    # trace JSON must continue to contain hashes and bounded metadata only.
    recording: dict[str, Any] | None = Field(default=None, exclude=True, repr=False)

    @model_validator(mode="after")
    def _synthetic_entry_has_no_provider_usage(self) -> ProgramCallTrace:
        if not self.physical_provider_call and (
            self.runtime_provider is not None
            or self.runtime_model is not None
            or self.runtime_model_sha256 is not None
            or self.runtime_binding_sha256 is not None
            or self.provider is not None
            or self.model is not None
            or self.model_sha256 is not None
            or self.error_detail is not None
            or self.output_sha256 is not None
            or self.validated_output is not None
            or bool(self.normalizations)
            or self.latency_ms != 0
            or self.input_tokens != 0
            or self.output_tokens != 0
            or self.cached_tokens != 0
            or self.total_tokens != 0
            or self.usage_coverage != "unknown"
            or self.provider_cost_microusd is not None
            or self.finish_reason is not None
            or self.terminal_disposition is not None
            or self.invocation_sha256 is not None
            or self.recording is not None
        ):
            raise ValueError("news_program_synthetic_call_provider_usage_invalid")
        return self


class ProgramTrace(_ExactContractModel):
    program_version: Literal["news_semantic_program_v13"]
    program_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    context_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    # The computed identity of everything the code decided about this call — request envelope, output
    # contract and schema, visible input shape, route budget and breaker (#314). It replaced a declared
    # `factory_id` the graph copied off the artifact: the stamp is now derived from the behavior it
    # names, so a deployment cannot move what the model sees while leaving this field still.
    envelope_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    event_semantics_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    taxonomy_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    # Which of the three Predictors answered, at judgment altitude (#651 §5.3). `taxonomy_sha256` is
    # `None` both when the route never got that far and when the taxonomy call failed on its own while the
    # other two answered; this names the second case, and it is the same code the persisted
    # `EditorialEnvelope` carries.
    taxonomy_error_code: str | None = None
    reader_card_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    verdict_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    editorial_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    answering_route: Literal["primary", "fallback"] | None = None
    fallback_from: str | None = None
    calls: tuple[ProgramCallTrace, ...] = ()

    @model_validator(mode="after")
    def _native_physical_calls_are_addressed_and_terminal(self) -> ProgramTrace:
        for call in self.calls:
            if not call.physical_provider_call:
                continue
            if (
                call.invocation_sha256 is None
                or call.terminal_disposition is None
                or call.runtime_provider is None
                or call.runtime_model is None
                or call.runtime_model_sha256 is None
                or call.runtime_binding_sha256 is None
            ):
                raise ValueError("news_program_native_call_audit_incomplete")
        return self


class ProgramUsage(_ExactContractModel):
    wall_latency_ms: int = Field(ge=0)
    call_count: int = Field(ge=0, le=12)
    physical_call_count: int = Field(default=0, ge=0, le=12)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cached_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    usage_coverage: Literal["complete", "partial", "unknown"] = "unknown"
    provider_cost_microusd: int | None = Field(default=None, ge=0)


def aggregate_program_usage(calls: Sequence[ProgramCallTrace]) -> dict[str, Any]:
    physical_calls = [call for call in calls if call.physical_provider_call]
    complete_cost = bool(physical_calls) and all(call.provider_cost_microusd is not None for call in physical_calls)
    coverage = (
        "complete"
        if physical_calls and all(call.usage_coverage == "complete" for call in physical_calls)
        else "unknown"
        if not physical_calls or all(call.usage_coverage == "unknown" for call in physical_calls)
        else "partial"
    )
    return {
        "call_count": len(calls),
        "physical_call_count": len(physical_calls),
        "input_tokens": sum(call.input_tokens for call in physical_calls),
        "output_tokens": sum(call.output_tokens for call in physical_calls),
        "cached_tokens": sum(call.cached_tokens for call in physical_calls),
        "total_tokens": sum(call.total_tokens for call in physical_calls),
        "usage_coverage": coverage,
        "provider_cost_microusd": (
            sum(cast(int, call.provider_cost_microusd) for call in physical_calls) if complete_cost else None
        ),
    }


class ScoredJudgment(_ExactContractModel):
    """Canonical verdict/editorial projection shared by every learning surface.

    Two shapes reach this model and only one of them is written here. `issue()` builds a judgment under
    the current contract. `from_stored()` -- and `model_validate` on a frozen corpus row -- reads one the
    ledger already holds, which may be `news_judgment_v2`: a verdict carrying `magnitude` and `audience`
    and no `fact_kind`, beside a `news_editorial_v3` envelope carrying the deleted `relevance` object.

    Those rows are not migrated and their hashes address the document the writer produced, so a
    reconstructed judgment keeps `verdict_sha256` and `scored_judgment_sha256` exactly as stored while its
    `verdict` and `editorial` are read in the current shape. The hashes are verified against the stored
    document *before* the retired fields are dropped, and `reconstructed_from` names the contract that
    wrote it so the round trip through the frozen corpus is stable and so no caller mistakes a
    reconstruction for a hash it can recompute (#679 review 1).
    """

    judgment_contract_version: Literal["news_judgment_v3", "news_judgment_v2"] = JUDGMENT_CONTRACT_VERSION
    verdict: TriageVerdict
    editorial: EditorialEnvelope
    verdict_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scored_judgment_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    # The contract the stored document was written under, when this judgment was read rather than issued.
    # `None` on everything the current Program produces.
    reconstructed_from: Literal["news_judgment_v2"] | None = None

    @classmethod
    def from_stored(
        cls,
        *,
        judgment_contract_version: str,
        verdict: Mapping[str, Any],
        editorial: Mapping[str, Any],
    ) -> ScoredJudgment:
        """One judgment as the ledger holds it, with the digests the writer computed over it.

        The two columns are hashed in the shape they are stored in, which is what makes the result
        comparable with the `judgment_sha256` column beside them. Validating them into the current models
        first would hash the *adapted* shape and no stored row would ever match again.
        """

        verdict_sha256 = canonical_sha(dict(verdict))
        payload = {
            "judgment_contract_version": judgment_contract_version,
            "verdict": dict(verdict),
            "editorial": dict(editorial),
            "verdict_sha256": verdict_sha256,
        }
        return cls.model_validate({**payload, "scored_judgment_sha256": canonical_sha(payload)})

    @model_validator(mode="before")
    @classmethod
    def _adapt_pre_cut_document(cls, value: Any) -> Any:
        if not isinstance(value, Mapping):
            return value
        version = str(value.get("judgment_contract_version") or "")
        if version != "news_judgment_v2" or value.get("reconstructed_from"):
            return value
        stored_verdict = value.get("verdict")
        payload = {
            "judgment_contract_version": version,
            "verdict": dict(stored_verdict) if isinstance(stored_verdict, Mapping) else stored_verdict,
            "editorial": value.get("editorial"),
            "verdict_sha256": value.get("verdict_sha256"),
        }
        if isinstance(stored_verdict, Mapping) and str(value.get("verdict_sha256") or "") != canonical_sha(
            dict(stored_verdict)
        ):
            raise ValueError("news_scored_judgment_identity_mismatch")
        if str(value.get("scored_judgment_sha256") or "") != canonical_sha(payload):
            raise ValueError("news_scored_judgment_identity_mismatch")
        return {**value, "reconstructed_from": "news_judgment_v2"}

    @classmethod
    def issue(cls, *, verdict: TriageVerdict, editorial: EditorialEnvelope) -> ScoredJudgment:
        verdict_sha256 = canonical_sha(verdict.model_dump(mode="json"))
        payload = {
            "judgment_contract_version": JUDGMENT_CONTRACT_VERSION,
            "verdict": verdict.model_dump(mode="json"),
            "editorial": editorial.model_dump(mode="json"),
            "verdict_sha256": verdict_sha256,
        }
        return cls(
            verdict=verdict,
            editorial=editorial,
            verdict_sha256=verdict_sha256,
            scored_judgment_sha256=canonical_sha(payload),
        )

    @model_validator(mode="after")
    def _projection_identity_is_exact(self) -> ScoredJudgment:
        # A reconstructed judgment's digests address the stored document, not this one, and were already
        # verified against it in `_adapt_pre_cut_document`. Recomputing them here would compare the v2
        # hash with a v3 canonicalization and fail every pre-cut row in the corpus.
        if self.reconstructed_from is not None:
            if self.judgment_contract_version != self.reconstructed_from:
                raise ValueError("news_scored_judgment_identity_mismatch")
            return self
        if self.judgment_contract_version != JUDGMENT_CONTRACT_VERSION:
            raise ValueError("news_scored_judgment_identity_mismatch")
        expected_verdict = canonical_sha(self.verdict.model_dump(mode="json"))
        payload = {
            "judgment_contract_version": self.judgment_contract_version,
            "verdict": self.verdict.model_dump(mode="json"),
            "editorial": self.editorial.model_dump(mode="json"),
            "verdict_sha256": self.verdict_sha256,
        }
        if self.verdict_sha256 != expected_verdict or self.scored_judgment_sha256 != canonical_sha(payload):
            raise ValueError("news_scored_judgment_identity_mismatch")
        return self


class SemanticJudgment(_ExactContractModel):
    verdict: TriageVerdict
    editorial: EditorialEnvelope
    program_version: str
    program_sha256: str
    trace: ProgramTrace
    usage: ProgramUsage
    answering_model: str | None = None
    fallback_from: str | None = None

    def scored(self) -> ScoredJudgment:
        return ScoredJudgment.issue(verdict=self.verdict, editorial=self.editorial)

    @model_validator(mode="after")
    def _trace_and_usage_match_judgment(self) -> SemanticJudgment:
        if (
            self.program_version != self.trace.program_version
            or self.program_sha256 != self.trace.program_sha256
            or self.fallback_from != self.trace.fallback_from
            or not self.answering_model
            or self.trace.answering_route is None
            or self.trace.answering_route != ("fallback" if self.fallback_from else "primary")
            or self.trace.event_semantics_sha256 is None
            or (self.trace.taxonomy_sha256 is None) != (self.editorial.taxonomy is None)
            or self.trace.taxonomy_error_code != self.editorial.taxonomy_error_code
            or self.trace.reader_card_sha256 is None
            or self.trace.verdict_sha256 != canonical_sha(self.verdict.model_dump(mode="json"))
            or self.trace.editorial_sha256 != self.editorial.editorial_sha256
        ):
            raise ValueError("news_program_judgment_trace_identity_mismatch")
        expected_usage = aggregate_program_usage(self.trace.calls)
        actual_usage = self.usage.model_dump(mode="json", exclude={"wall_latency_ms"})
        if actual_usage != expected_usage:
            raise ValueError("news_program_judgment_usage_mismatch")
        return self


class SemanticJudgeError(Exception):
    """Declared failure mode of the semantic-judgment Interface."""

    def __init__(
        self,
        code: str,
        *,
        retryable: bool,
        output_failure: bool,
        attempts: int,
        partial_trace: ProgramTrace | None,
        finish_reason: str | None = None,
        failing_predictor: str | None = None,
        primary_code: str | None = None,
    ) -> None:
        self.code = code
        self.retryable = retryable
        self.output_failure = output_failure
        self.attempts = attempts
        self.partial_trace = partial_trace
        self.finish_reason = finish_reason
        self.failing_predictor = failing_predictor
        self.primary_code = primary_code
        super().__init__(code)


@runtime_checkable
class SemanticJudge(Protocol):
    async def judge(self, context: TriageContext) -> SemanticJudgment: ...


__all__ = [
    "CATALOG_CANDIDATE_CLASSES_MAX",
    "CATALOG_CANDIDATE_SYMBOLS_MAX",
    "EDITORIAL_CONTRACT_VERSION",
    "FACT_KINDS",
    "GROUNDED_ASSETS_MAX",
    "JUDGMENT_CONTRACT_VERSION",
    "STRATEGIES_MAX",
    "WATCHLIST_MAX",
    "CatalogCandidate",
    "EditorialEnvelope",
    "FactKind",
    "FrozenEventEvidence",
    "ModelVisibleCardInput",
    "ModelVisibleSemanticsInput",
    "ModelVisibleTaxonomyInput",
    "ProgramCallTrace",
    "ProgramNormalizationTrace",
    "ProgramTrace",
    "ProgramUsage",
    "ReaderCardSemanticView",
    "ScoredJudgment",
    "SemanticGateContext",
    "SemanticJudge",
    "SemanticJudgeError",
    "SemanticJudgment",
    "TriageContext",
    "aggregate_program_usage",
    "catalog_candidates_of",
    "unambiguous_catalog_class",
]
