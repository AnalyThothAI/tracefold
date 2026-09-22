"""Model-drafted `news_review_v7` rubrics, for an authorized reviewer to accept or reject (#117, #148).

Two facts set the whole shape of this module.

First, gold coverage is 0.1226 and *all* of it is `novelty` — no accepted review states a correct
`magnitude`, `direction` or `assets`, because `expected` only landed in #143 and nobody has used it. Without
gold, a failed dimension scores on "did anything change", so an optimizer can bank points by changing a value
to another wrong one. Stating the right answer is worth more than any amount of extra compute.

Second, `ReviewDesk.submit` writes an `acceptance` row unconditionally — there is no draft state. Anything
written through that path is accepted release evidence the instant it lands.

Therefore a draft is **not** a review and never touches `news_reviews`. This produces a file. An
owner-authorized reviewer reads it, edits what is wrong, and submits the approved subset through the existing
review writer. The model's job is to turn "compose a judgment from scratch" into "confirm or reject one", not
to acquire acceptance authority from drafting alone.

Third (#501 D8, #534): taxonomy Gold is drafted *blind*, twice. The rubric drafter used to label taxonomy
while reading `card_json`, which carried Stable's own taxonomy, so the Gold measured which batch drafted it.
Now two drafters, taken only from the routes the machine already has — `qwen3.8-27b:thinking` (local),
`deepseek-v4-pro`, `deepseek-v4-flash`, two different names, no third family — each read only the Program's
own bounded taxonomy input — evidence and Gate facts, no card, no Stable output, no told ledger, no review —
through the very Signature and seed the taxonomy Predictor runs. The non-thinking `qwen3.8-27b` is not a
drafter, because it *is* the Stable taxonomy route: same seed, same `evidence_json`, temperature 0, so its
label is already in the verdict and readiness reports `stable_exact_n / stable_mismatch_n` for free.
Agreement is the draft; disagreement is recorded, the draft takes drafter A, and the accepting reviewer
decides.
"""

from __future__ import annotations

import importlib.metadata
import threading
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from typing import Any, Final, Literal, TypedDict, cast

import dspy  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..artifact_identity import canonical_sha
from ..models import MarketType, market_type_of
from ..program.contracts import (
    ReaderValue,
    TradeAffectedMarket,
    TradeChannel,
    TradeDevelopmentDelta,
    TradeImpactBreadth,
    TradeSurprise,
    TradeTradability,
)
from ..program.lm import StructuredOutputMode, physical_call_usage, structured_output_capability
from ..program.seed import SEED_INSTRUCTIONS
from ..program.signatures import EventTaxonomySignature
from ..taxonomy import ModelTaxonomyV1, SourceAuthority

_EMPTY_SPEND: Final = {
    "calls": 0,
    "input_tokens": 0,
    "output_tokens": 0,
    "cached_tokens": 0,
    "total_tokens": 0,
    "observed_cost_microusd": 0,
    "cost_unknown_calls": 0,
}


def spend_by_model(*drafters: Any) -> dict[str, dict[str, int]]:
    """Per-model physical calls, tokens and observed/unknown cost, merged across drafters of one batch."""

    return merge_spend(
        *(
            {str(getattr(drafter, "model", "") or "unknown"): dict(spend)}
            for drafter in drafters
            if isinstance(spend := getattr(getattr(drafter, "_lm", None), "spend", None), Mapping)
        )
    )


def merge_spend(*maps: Mapping[str, Mapping[str, Any]] | None) -> dict[str, dict[str, int]]:
    """Add per-model spend maps together. One model drafting two roles spends twice, not once."""

    merged: dict[str, dict[str, int]] = {}
    for source in maps:
        for model, spend in dict(source or {}).items():
            bucket = merged.setdefault(str(model), dict(_EMPTY_SPEND))
            for field, value in dict(spend).items():
                bucket[field] = bucket.get(field, 0) + int(value)
    return merged


class ConfiguredDrafterLM(dspy.LM):  # type: ignore[misc]
    """Stock DSPy LM with the endpoint's explicit structured-output capability, and a spend counter.

    The counter exists because the daily audit runs on a paid route (#675 §4): a batch that cannot say how
    many physical calls it made, on what tokens, at what observed cost is a batch nobody can budget. It
    reuses `physical_call_usage`, the Program ledger's own accounting, so there is one reader of provider
    usage rather than two. `cost_unknown_calls` counts the calls whose provider stated no cost — an unknown
    cost is reported as unknown, never as zero.
    """

    def __init__(self, model: str, *, structured_output: StructuredOutputMode, **kwargs: Any) -> None:
        self._structured_output = structured_output
        self._spend_lock = threading.Lock()
        self.spend: dict[str, int] = dict(_EMPTY_SPEND)
        super().__init__(model, **kwargs)

    def _process_lm_response(self, response: Any, prompt: Any, messages: Any, **kwargs: Any) -> Any:
        """The one funnel every sync and async delegate answer passes through."""

        usage = physical_call_usage(response)
        cost = usage["cost_microusd"]
        with self._spend_lock:
            self.spend["calls"] += 1
            for field in ("input_tokens", "output_tokens", "cached_tokens", "total_tokens"):
                self.spend[field] += int(usage[field] or 0)
            if cost is None:
                self.spend["cost_unknown_calls"] += 1
            else:
                self.spend["observed_cost_microusd"] += int(cost)
        return super()._process_lm_response(response, prompt, messages, **kwargs)

    @property
    def supported_params(self) -> set[str]:
        return set(structured_output_capability(self._structured_output)["supported_params"])

    @property
    def supports_response_schema(self) -> bool:
        return bool(structured_output_capability(self._structured_output)["supports_response_schema"])


def build_drafter_lm(
    *,
    model_name: str,
    api_key: str,
    api_base: str,
    model_kwargs: Mapping[str, Any],
    structured_output: StructuredOutputMode,
    temperature: float | None = 0,
    timeout: float = 120.0,
    max_tokens: int = 4_096,
    lm_type: type[ConfiguredDrafterLM] = ConfiguredDrafterLM,
) -> ConfiguredDrafterLM:
    """The drafting endpoint. `model_kwargs` comes from the app's provider resolution.

    Passing it matters: for `deepseek-v4-*` it carries `extra_body.thinking = disabled`, and this gateway
    enables thinking by default. Without it the model spends its whole output budget reasoning and returns an
    empty answer — which is what made every early draft fail to parse. Raising `max_tokens` only hid that.

    It lives here rather than in `learning/baseline.py`, beside the draft-only
    proposal contract it serves. The Program and optimizer use their own native
    DSPy seams and do not route through this review helper.
    """

    request: dict[str, Any] = {
        "api_key": str(api_key),
        "api_base": str(api_base),
        "timeout": float(timeout),
        "max_tokens": int(max_tokens),
        "cache": False,
        "num_retries": 0,
        **dict(model_kwargs),
    }
    if temperature is not None:
        request["temperature"] = temperature
    return lm_type(str(model_name), structured_output=structured_output, **request)


# How many tasks `build_draft_batch` keeps in flight by default (#675 §4). Four is a width the local
# route and a hosted one both sustain; the CLI's `--concurrency` moves it, and 1 is the old serial loop.
DEFAULT_DRAFT_CONCURRENCY: Final = 4

DRAFTER_ID = "tracefold.news.review_drafter_v7"
TAXONOMY_BLIND_DRAFTER_ID = "tracefold.news.taxonomy_blind_drafter_v1"
# v5 (#501): the rubric drafter no longer labels taxonomy; each entry carries two blind taxonomy drafts,
# the chosen draft, and a disagreement flag, and the batch names both blind drafters.
# v6 (#548 PR-B.1): each entry also carries `stable_taxonomy`, the label the code-written taxonomy_*
# dimensions are computed against. Without it, accepting a draft could only copy the dimensions drafting
# time wrote, which a reviewer's taxonomy edit had already made wrong. A v5 file carries no such field and
# is refused by the schema check rather than accepted with stale dimensions.
ReviewDraftBatchSchema = Literal["tracefold.news.review_draft_batch.v6"]
DRAFT_SCHEMA: Final[ReviewDraftBatchSchema] = "tracefold.news.review_draft_batch.v6"

_INSTRUCTION = """You are drafting a quality review of one already-published Chinese news card for a
crypto/US-equity trading desk. An owner-authorized reviewer will accept or reject your draft; never assume it
is final.

You see the original evidence and the card the system produced. Judge the card against the evidence only.

For each dimension answer pass / fail / not_applicable:
- factual_fidelity: does the card contradict the evidence on any number, entity, direction or causal link?
- headline_fidelity: does headline_zh faithfully carry the original headline's subject, action and every
  decision-relevant number? Condensing is fine; dropping a number or the consequence clause is not.
- asset_grounding: are the primary assets actually what the text is about? A provider tag is a lead, not a
  fact. Naming an instrument the text does not concern is a fail.
- direction: is the price implication right for the named assets? `neutral`/`unclear` is correct when the
  evidence does not imply a clear direction; forcing a sign is a fail.
- magnitude: 0 irrelevant/marketing; 1 a routine update that changes nothing about what the name sells,
  builds or earns; 2 clearly tradable (earnings, a company's own product/capacity/pricing move, listing or
  delisting, regulation landing, security incident, notable ETF flow, macro well off consensus); 3 macro
  turning point, systemic risk or geopolitical escalation.
- timeliness: not_applicable unless the evidence shows the card was late enough to matter.
- trade_impact_breadth: none / single_instrument / sector / regional / cross_asset / global_systemic.
- trade_tradability: direct / second_order / contextual / none.
- trade_surprise: unscheduled / material_vs_expectation / in_line / unknown.
- trade_development_delta: state_change / material_detail / color_only / scheduled.
- trade_channels: the exact supported causal channels, with no inferred extras.
- trade_affected_markets: the exact directly or causally affected market surfaces.
- reader_value: escalate / realtime / background / none under the typed trade-attention contract.

Do NOT judge why_support or why_value. Leave them out of `dimensions` entirely — the accepting reviewer writes
those. Measured against 25 human-reviewed Events you agree with a reviewer 76-88% of the time on the
dimensions above, but only 42-43% on those two, and 27 of 46 total false failures came from them alone.

explanation: optional, and it is evidence rather than a verdict. `source_spans` are verbatim excerpts copied
character for character out of the evidence you were shown — at most 8, each one a span that carries a
decision-relevant claim. `key_facts` are at most 6 short statements a correct card must keep. Propose nothing
else in this block: the error types, the forbidden claims and the reference wording are the reviewer's, and a
span you paraphrased instead of copied is refused at submit.

should_push: must_push / should_push / should_hold / must_hold / uncertain — whether a trader needed this.
Reserve `must_*` for cases where the opposite decision would be a real failure (a security incident missed,
marketing pushed).

novelty: new_fact / progression / restatement, judged against the told ledger you are shown. Use
`restatement` only when a told entry carries the same fact, and then name that entry's event_id.

expected: ONLY for dimensions you marked fail, state the exact correct value — including every failed typed
trade-relevance dimension. Leave a field out when you are not confident. Every asset you state carries
market_type from crypto / equity / commodity / index / fx / pre_ipo / unknown: a ticker without its market is
not an answer, because the same three letters name a token and a listed company. Say unknown rather than pick
a market the evidence does not establish.
This is the most valuable part of the draft: "wrong" without "and the answer is X" teaches nothing.

Do NOT label taxonomy (subject codes, event family, change state, assertion status) and do not judge the
taxonomy_* dimensions: two blind drafters label taxonomy from the evidence alone, and code compares them
with the system's own label.

confidence: 0.0-1.0, how sure you are the accepting reviewer would agree with this draft.
reasoning: one short sentence a reviewer can check quickly."""


class DraftAsset(BaseModel):
    """Mirrors `desk.ExpectedAsset`, market included (#651 §6.2)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str = Field(min_length=1, max_length=32)
    market_type: MarketType
    role: Literal["primary", "mentioned"] = "primary"

    @model_validator(mode="before")
    @classmethod
    def _market_is_vocabulary_or_unknown(cls, value: Any) -> Any:
        if isinstance(value, Mapping):
            return {**value, "market_type": market_type_of(value.get("market_type"))}
        return value


class DraftExpected(BaseModel):
    """Mirrors `review.ExpectedCorrection`. Only failed dimensions may carry a value."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    magnitude: int | None = Field(default=None, ge=0, le=3)
    direction: Literal["bullish", "bearish", "neutral", "unclear"] | None = None
    assets: list[DraftAsset] | None = Field(default=None, max_length=16)
    trade_impact_breadth: TradeImpactBreadth | None = None
    trade_tradability: TradeTradability | None = None
    trade_surprise: TradeSurprise | None = None
    trade_development_delta: TradeDevelopmentDelta | None = None
    trade_channels: list[TradeChannel] | None = Field(default=None, max_length=4)
    trade_affected_markets: list[TradeAffectedMarket] | None = Field(default=None, max_length=4)
    reader_value: ReaderValue | None = None


class DraftExplanation(BaseModel):
    """The two halves of `ExplanationCorrectionV1` a model is allowed to propose (#651 §7.2).

    Evidence, not verdicts. Copying a span out of the source and listing the facts a card must keep are
    extraction tasks, which is what the measured 76-88% agreement covers; deciding whether the card's
    reasoning *supports* its claim is the 42-43% one, and it stays with the reviewer. The other three
    fields of the block — forbidden claims, error types, reference wording — are the reviewer's
    conclusions about a defect, so no drafter writes them.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_spans: list[str] = Field(default_factory=list, max_length=8)
    key_facts: list[str] = Field(default_factory=list, max_length=6)


class DraftNovelty(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    judgment: Literal["new_fact", "progression", "restatement", "uncertain"]
    duplicate_of: str = Field(default="", max_length=128)


DraftDimensionLabel = Literal["pass", "fail", "not_applicable"]


class DraftDimensions(TypedDict, total=False):
    factual_fidelity: DraftDimensionLabel
    headline_fidelity: DraftDimensionLabel
    asset_grounding: DraftDimensionLabel
    direction: DraftDimensionLabel
    magnitude: DraftDimensionLabel
    timeliness: DraftDimensionLabel
    trade_impact_breadth: DraftDimensionLabel
    trade_tradability: DraftDimensionLabel
    trade_surprise: DraftDimensionLabel
    trade_development_delta: DraftDimensionLabel
    trade_channels: DraftDimensionLabel
    trade_affected_markets: DraftDimensionLabel
    reader_value: DraftDimensionLabel


# The four taxonomy dimensions are written by code (#501): Stable's persisted label against the blind
# draft, axis by axis. There is no fifth: `taxonomy_source_authority` compared a code fact with itself and
# was always `pass`, so #651 deleted it from the rubric rather than keep publishing a constant.
TAXONOMY_DIMENSION_AXES: Final[tuple[tuple[str, str], ...]] = (
    ("taxonomy_subject_codes", "subject_codes"),
    ("taxonomy_event_family", "event_family"),
    ("taxonomy_change_state", "change_state"),
    ("taxonomy_assertion_status", "assertion_status"),
)
TAXONOMY_DIMENSIONS: Final[tuple[str, ...]] = tuple(dimension for dimension, _axis in TAXONOMY_DIMENSION_AXES)

# What the drafter is allowed to judge, measured rather than assumed. Agreement with a human reviewer over 25
# Events they both saw: direction 88%, factual_fidelity 84%, headline_fidelity 84%, magnitude 76%,
# asset_grounding 70% — against why_support 43% and why_value 42%. Those two also produced 27 of the 46
# "human passed it, the draft failed it" disagreements, so letting the model touch them would push a large
# number of failures a reviewer disagrees with into the corpus, where the optimizer would learn from them.
# They also carry no gold: "the correct Chinese sentence" is not a value a rubric can hold.
DRAFTABLE_DIMENSIONS = frozenset(DraftDimensions.__annotations__) | frozenset(TAXONOMY_DIMENSIONS)


class ReviewDimensions(DraftDimensions, total=False):
    """The rubric dimensions plus the four taxonomy dimensions code writes after the blind drafts."""

    taxonomy_subject_codes: DraftDimensionLabel
    taxonomy_event_family: DraftDimensionLabel
    taxonomy_change_state: DraftDimensionLabel
    taxonomy_assertion_status: DraftDimensionLabel


class RubricDraft(BaseModel):
    """What the rubric model proposes: every dimension except taxonomy, which it never sees or labels."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    should_push: Literal["must_push", "should_push", "should_hold", "must_hold", "uncertain"]
    dimensions: DraftDimensions
    novelty: DraftNovelty
    expected: DraftExpected | None = None
    explanation: DraftExplanation | None = None
    expected_correction: str = Field(default="", max_length=2_000)
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str = Field(default="", max_length=1_000)


class ReviewDraft(RubricDraft):
    """The rubric draft plus the code-assembled taxonomy. Shaped so a reviewer edit becomes a submission.

    `taxonomy` is drafter A's label, or the agreed label when both blind drafters agree; `taxonomy_drafts`
    keeps both under their model names; `taxonomy_disagreement` marks the tasks the accepting reviewer must
    decide. The taxonomy_* dimensions inside `dimensions` are code-written from Stable against `taxonomy`.
    """

    dimensions: ReviewDimensions
    taxonomy: ModelTaxonomyV1
    taxonomy_drafts: dict[str, ModelTaxonomyV1] = Field(default_factory=dict)
    taxonomy_disagreement: bool = False

    @field_validator("taxonomy", mode="before")
    @classmethod
    def _discard_computed_source_authority(cls, value: Any) -> Any:
        if not isinstance(value, Mapping) or "source_authority" not in value:
            return value
        return {name: label for name, label in value.items() if name != "source_authority"}


class _ReviewDraftSignature(dspy.Signature):  # type: ignore[misc]
    evidence_json: str = dspy.InputField(desc="Bounded original evidence for the Event")
    card_json: str = dspy.InputField(desc="The verdict and card the system produced")
    told_json: str = dspy.InputField(desc="Cards already sent in the 4 h window, newest first")
    draft: RubricDraft = dspy.OutputField(desc="Proposed rubric; an owner-authorized reviewer decides")


class DraftedReview(BaseModel):
    """One draft plus everything an authorized reviewer needs to accept it through ReviewDesk.

    `stable_taxonomy` is Stable's persisted label — the other side of the code-written taxonomy_*
    dimensions — and is required rather than defaulted (#548 PR-B.1): the reviewer may edit `draft.taxonomy`
    before accepting, and the dimensions can only be recomputed against a label the entry actually carries.
    `None` is the real answer when Stable never labelled the Event, and yields `not_applicable` on every
    axis, exactly as it did at drafting time.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str
    task_version: str
    event_id: str
    headline_zh: str
    source_authority: SourceAuthority
    draft: ReviewDraft
    stable_taxonomy: ModelTaxonomyV1 | None
    error: str | None = None


class ReviewDraftBatch(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_id: ReviewDraftBatchSchema = DRAFT_SCHEMA
    drafter: dict[str, Any]
    taxonomy_drafters: dict[str, Any]
    drafts: tuple[DraftedReview, ...]

    @property
    def batch_sha256(self) -> str:
        return canonical_sha(self.model_dump(mode="json"))


class TaxonomyBlindDrafter:
    """One blind taxonomy label per Event, through the Program's own Signature and seed.

    It accepts only the rendered taxonomy evidence — `ModelVisibleTaxonomyInput` has no card, no Stable
    output, no told ledger and no review — so the bytes the drafter reads are the bytes the Predictor reads.
    """

    def __init__(self, lm: dspy.LM, *, max_tokens: int = 16_384) -> None:
        self._lm = lm
        self._predict = dspy.Predict(
            EventTaxonomySignature.with_instructions(SEED_INSTRUCTIONS["taxonomy"]),
            temperature=0,
            max_tokens=max_tokens,
        )
        self.calls = 0
        self.failures = 0
        self._counter_lock = threading.Lock()

    @property
    def model(self) -> str:
        return str(getattr(self._lm, "model", "") or "")

    @property
    def identity(self) -> dict[str, Any]:
        signature = EventTaxonomySignature.with_instructions(SEED_INSTRUCTIONS["taxonomy"])
        return {
            "drafter_id": TAXONOMY_BLIND_DRAFTER_ID,
            "model": self.model,
            "dspy_version": importlib.metadata.version("dspy"),
            "instruction_sha256": canonical_sha(SEED_INSTRUCTIONS["taxonomy"]),
            "signature_sha256": canonical_sha(signature.dump_state()),
            "input": "ModelVisibleTaxonomyInput: event and gate only",
        }

    def draft(self, *, evidence_json: str) -> ModelTaxonomyV1 | str:
        """Return a taxonomy, or an error string. Never raises: one bad Event must not end the batch."""

        with self._counter_lock:
            self.calls += 1
        try:
            with dspy.context(lm=self._lm, adapter=dspy.JSONAdapter(use_native_function_calling=False)):
                prediction = self._predict(evidence_json=evidence_json)
            taxonomy = prediction.taxonomy
            return taxonomy if isinstance(taxonomy, ModelTaxonomyV1) else ModelTaxonomyV1.model_validate(taxonomy)
        except Exception as exc:
            with self._counter_lock:
                self.failures += 1
            return f"{type(exc).__name__}: {str(exc)[:200]}"


def taxonomy_dimensions(stable: ModelTaxonomyV1 | Mapping[str, Any] | None, draft: ModelTaxonomyV1) -> dict[str, str]:
    """Code-written pass/fail per axis: Stable's persisted label against the blind draft."""

    if stable is None:
        return dict.fromkeys(TAXONOMY_DIMENSIONS, "not_applicable")
    recorded = stable if isinstance(stable, ModelTaxonomyV1) else ModelTaxonomyV1.model_validate(_model_axes(stable))
    return {
        dimension: "pass" if getattr(recorded, axis) == getattr(draft, axis) else "fail"
        for dimension, axis in TAXONOMY_DIMENSION_AXES
    }


def _model_axes(value: Mapping[str, Any]) -> dict[str, Any]:
    return {field: value[field] for field in ModelTaxonomyV1.model_fields if field in value}


class ReviewDrafter:
    """One bounded model call per Event. Reads nothing, writes nothing — it returns proposals."""

    # A rubric is a small object, but a reasoning model spends its output budget thinking first and only then
    # emits the answer. At 1,024 every call came back with `text: ''` and the whole rubric stranded in
    # `reasoning_content` — 6/6, then 4/4, reported as parse failures rather than as a budget problem.
    def __init__(self, lm: dspy.LM, *, max_tokens: int = 16_384) -> None:
        self._lm = lm
        self._predict = dspy.Predict(
            _ReviewDraftSignature.with_instructions(_INSTRUCTION),
            temperature=0,
            max_tokens=max_tokens,
        )
        self.calls = 0
        self.failures = 0
        self._counter_lock = threading.Lock()

    @property
    def model(self) -> str:
        return str(getattr(self._lm, "model", "") or "")

    @property
    def identity(self) -> dict[str, Any]:
        signature = _ReviewDraftSignature.with_instructions(_INSTRUCTION)
        adapter = dspy.JSONAdapter(use_native_function_calling=False)
        rendered = adapter.format(
            signature,
            [],
            {"evidence_json": "{}", "card_json": "{}", "told_json": "[]"},
        )
        supported = tuple(sorted(str(value) for value in (getattr(self._lm, "supported_params", ()) or ())))
        return {
            "drafter_id": DRAFTER_ID,
            "model": self.model,
            "dspy_version": importlib.metadata.version("dspy"),
            "instruction_sha256": canonical_sha(_INSTRUCTION),
            "output_schema_sha256": canonical_sha(ReviewDraft.model_json_schema()),
            "signature_sha256": canonical_sha(signature.dump_state()),
            "adapter_render_sha256": canonical_sha(rendered),
            "structured_output_capability": {
                "source": "configured_endpoint.structured_output",
                "response_format": "response_format" in supported,
                "supports_response_schema": bool(getattr(self._lm, "supports_response_schema", False)),
            },
            "authority": "proposal_only; an owner-authorized reviewer accepts it through ReviewDesk.submit",
        }

    def draft(self, *, evidence_json: str, card_json: str, told_json: str) -> RubricDraft | str:
        """Return a rubric draft, or an error string. Never raises: one bad Event must not end the batch."""

        with self._counter_lock:
            self.calls += 1
        try:
            with dspy.context(lm=self._lm, adapter=dspy.JSONAdapter(use_native_function_calling=False)):
                prediction = self._predict(
                    evidence_json=evidence_json,
                    card_json=card_json,
                    told_json=told_json,
                )
            draft = prediction.draft
            return draft if isinstance(draft, RubricDraft) else RubricDraft.model_validate(draft)
        except Exception as exc:
            with self._counter_lock:
                self.failures += 1
            return f"{type(exc).__name__}: {str(exc)[:200]}"


def submission_payload(
    draft: ReviewDraft,
    *,
    stable_taxonomy: ModelTaxonomyV1 | Mapping[str, Any] | None,
    draft_author: str = DRAFTER_ID,
) -> dict[str, Any]:
    """The `EventRubricSubmission` a reviewer would send after accepting this draft, unchanged.

    Built here so the accept step never has to reshape model output by hand, and so the rubric's own
    validators — gold only on failed dimensions, evidence refs required for a fail — are what decide whether
    a draft is submittable at all.

    `stable_taxonomy` is required, not optional (#548 PR-B.1). The five taxonomy_* dimensions are code
    written from Stable's label against the draft's, and this function emits the *edited* `draft.taxonomy`.
    Copying the labels drafting time computed therefore published a comparison of a label the reviewer had
    already replaced — an edit that made the draft agree with Stable still submitted `fail`. They are
    recomputed here, through the same `taxonomy_dimensions`, so the row states the comparison it names.
    """

    # Every label the rubric accepts, `not_applicable` included. Filtering to pass/fail looks tidier and is
    # wrong: `should_push` of `must_push`/`should_push` *requires* a `timeliness` entry, and that entry is
    # `not_applicable` on most Events — dropping it makes the majority of drafts unsubmittable.
    # A model that answers anyway is silently dropped rather than trusted: the reviewer owns `why_*`.
    dimensions = {name: label for name, label in draft.dimensions.items() if name in DRAFTABLE_DIMENSIONS}
    dimensions.update(taxonomy_dimensions(stable_taxonomy, draft.taxonomy))
    expected = draft.expected.model_dump(mode="json", exclude_none=True) if draft.expected else {}
    # `NoveltyJudgment` requires `duplicate_of` on a restatement and forbids it anywhere else. A model that
    # names the told entry while judging `new_fact` — or calls a restatement without naming one — would
    # otherwise produce a draft no reviewer can accept, with no repair path.
    novelty = draft.novelty.model_dump(mode="json")
    if novelty["judgment"] != "restatement":
        novelty["duplicate_of"] = ""
    elif not str(novelty.get("duplicate_of") or "").strip():
        # The claim cannot be checked without a target, so it is downgraded rather than dropped: a reviewer
        # still sees the model thought this was a repeat, in the one field they will read.
        novelty = {"judgment": "uncertain", "duplicate_of": ""}
    # The four model axes, not the persisted taxonomy (#651 §7.2). `source_authority` is derived from the
    # evidence by code on the read side; issuing it here made the drafter look like it had labelled it.
    taxonomy = draft.taxonomy
    # Taxonomy provenance names the blind drafters, never the rubric model, which did not label it.
    taxonomy_review: dict[str, Any] = {
        "label_source": "model_draft",
        "draft_author": "+".join(sorted(draft.taxonomy_drafts)) if draft.taxonomy_drafts else draft_author,
        "review_role": "primary",
        "draft_taxonomy": taxonomy.model_dump(mode="json"),
    }
    if draft.taxonomy_drafts:
        taxonomy_review["drafts"] = {
            model: label.model_dump(mode="json") for model, label in sorted(draft.taxonomy_drafts.items())
        }
    payload: dict[str, Any] = {
        "kind": "event_rubric",
        "should_push": draft.should_push,
        "dimensions": dimensions,
        "novelty": novelty,
        "expected_correction": draft.expected_correction,
        "taxonomy": taxonomy.model_dump(mode="json"),
        "taxonomy_review": taxonomy_review,
    }
    if expected:
        payload["expected"] = expected
    if draft.explanation is not None and (draft.explanation.source_spans or draft.explanation.key_facts):
        # Proposal only, and the rubric checks it: a span the frozen evidence does not contain is refused
        # at submit rather than stored as a citation nobody can follow.
        payload["explanation"] = draft.explanation.model_dump(mode="json")
    if any(label == "fail" for label in dimensions.values()):
        # The rubric refuses a `fail` without one; the drafter cannot invent a reviewer's citation, so it
        # points at the two things it did read.
        payload["evidence_refs"] = ["source:leader:title", f"draft:{draft_author}"]
    return payload


@dataclass(frozen=True)
class _DraftOutcome:
    """One task's result, carried out of the worker so the counters are folded in task order, not arrival order."""

    entry: DraftedReview
    labelled: bool = False
    agreed: bool = False
    stable_hit_a: bool = False
    stable_hit_b: bool = False


def build_draft_batch(
    drafter: ReviewDrafter,
    tasks: Sequence[Mapping[str, Any]],
    *,
    taxonomy_drafters: tuple[TaxonomyBlindDrafter, TaxonomyBlindDrafter],
    concurrency: int = DEFAULT_DRAFT_CONCURRENCY,
) -> ReviewDraftBatch:
    """Draft one review per task: two blind taxonomy labels, then the rubric.

    Each task carries `taxonomy_evidence_json` (the Program's bounded taxonomy input), the three rubric
    inputs, `stable_taxonomy` (Stable's persisted label, for the code-written taxonomy dimensions) and its
    identity.

    `concurrency` is how many tasks may be in flight at once (#675 §4). Three sequential model calls per
    task made a 300-task daily batch a 5-14 hour job on the local route, which is the same as saying the
    daily audit does not run. Each task is still independent: its own failure still becomes that entry's
    `error` and ends nothing, and the batch is folded back in task order, so output and counters do not
    depend on which call returned first.
    """

    task_ids = [str(task["task_id"]) for task in tasks]
    if len(task_ids) != len(set(task_ids)):
        raise ValueError("news_review_drafter_duplicate_task")
    drafter_a, drafter_b = taxonomy_drafters
    if drafter_a.model == drafter_b.model:
        raise ValueError("news_review_taxonomy_drafters_must_differ")
    drafts: list[DraftedReview] = []
    agreement_n = 0
    stable_agreement = {drafter_a.model: 0, drafter_b.model: 0}
    labelled_n = 0
    disagreement_task_ids: list[str] = []

    def draft_one(task: Mapping[str, Any]) -> _DraftOutcome:
        task_id = str(task["task_id"])
        identity = {
            "task_id": task_id,
            "task_version": str(task["task_version"]),
            "event_id": str(task["event_id"]),
            "headline_zh": str(task.get("headline_zh") or ""),
            "source_authority": cast(SourceAuthority, str(task.get("source_authority") or "unknown")),
        }
        # Read before the first model call and carried on every entry: the accept step recomputes the
        # taxonomy_* dimensions from it, so a batch that lost it could only publish a stale comparison.
        stable_raw = task.get("stable_taxonomy")
        stable = ModelTaxonomyV1.model_validate(_model_axes(stable_raw)) if isinstance(stable_raw, Mapping) else None
        blind_input = str(task["taxonomy_evidence_json"])
        label_a = drafter_a.draft(evidence_json=blind_input)
        label_b = drafter_b.draft(evidence_json=blind_input)
        if not isinstance(label_a, ModelTaxonomyV1) or not isinstance(label_b, ModelTaxonomyV1):
            failed = label_a if not isinstance(label_a, ModelTaxonomyV1) else label_b
            return _DraftOutcome(
                entry=DraftedReview(
                    **identity,
                    draft=_EMPTY_DRAFT,
                    stable_taxonomy=stable,
                    error=f"taxonomy_drafting_failed: {failed}",
                )
            )
        agreed = label_a == label_b
        outcome = drafter.draft(
            evidence_json=str(task["evidence_json"]),
            card_json=str(task["card_json"]),
            told_json=str(task.get("told_json") or "[]"),
        )
        counted = _DraftOutcome(
            entry=DraftedReview(**identity, draft=_EMPTY_DRAFT, stable_taxonomy=stable, error="placeholder"),
            labelled=True,
            agreed=agreed,
            stable_hit_a=stable is not None and label_a == stable,
            stable_hit_b=stable is not None and label_b == stable,
        )
        if not isinstance(outcome, RubricDraft):
            return replace(
                counted,
                entry=DraftedReview(**identity, draft=_EMPTY_DRAFT, stable_taxonomy=stable, error=outcome),
            )
        review_draft = ReviewDraft(
            **outcome.model_dump(mode="json", exclude={"dimensions"}),
            dimensions=cast(ReviewDimensions, {**dict(outcome.dimensions), **taxonomy_dimensions(stable, label_a)}),
            taxonomy=label_a,
            taxonomy_drafts={drafter_a.model: label_a, drafter_b.model: label_b},
            taxonomy_disagreement=not agreed,
        )
        return replace(counted, entry=DraftedReview(**identity, draft=review_draft, stable_taxonomy=stable))

    width = max(1, int(concurrency))
    if width == 1:
        outcomes = [draft_one(task) for task in tasks]
    else:
        with ThreadPoolExecutor(max_workers=width, thread_name_prefix="news-draft") as pool:
            outcomes = list(pool.map(draft_one, tasks))
    for task_id, outcome_row in zip(task_ids, outcomes, strict=True):
        drafts.append(outcome_row.entry)
        if not outcome_row.labelled:
            continue
        labelled_n += 1
        agreement_n += bool(outcome_row.agreed)
        if not outcome_row.agreed:
            disagreement_task_ids.append(task_id)
        stable_agreement[drafter_a.model] += outcome_row.stable_hit_a
        stable_agreement[drafter_b.model] += outcome_row.stable_hit_b
    return ReviewDraftBatch(
        drafter={
            **drafter.identity,
            "calls": drafter.calls,
            "failures": drafter.failures,
            # Physical provider calls, not rubric attempts: a retried or reformatted call is spend too.
            "spend": spend_by_model(drafter),
        },
        taxonomy_drafters={
            "models": [drafter_a.model, drafter_b.model],
            "identities": [drafter_a.identity, drafter_b.identity],
            "calls": drafter_a.calls + drafter_b.calls,
            "failures": drafter_a.failures + drafter_b.failures,
            "labelled_n": labelled_n,
            "agreement_n": agreement_n,
            "agreement_rate": round(agreement_n / labelled_n, 6) if labelled_n else None,
            "stable_agreement_rate": {
                model: (round(count / labelled_n, 6) if labelled_n else None)
                for model, count in stable_agreement.items()
            },
            "disagreement_task_ids": disagreement_task_ids,
            "disagreement_policy": "draft takes drafter A; the accepting reviewer decides",
            "spend": spend_by_model(drafter_a, drafter_b),
        },
        drafts=tuple(drafts),
    )


_EMPTY_DRAFT = ReviewDraft(
    should_push="uncertain",
    dimensions={
        "taxonomy_subject_codes": "not_applicable",
        "taxonomy_event_family": "not_applicable",
        "taxonomy_change_state": "not_applicable",
        "taxonomy_assertion_status": "not_applicable",
    },
    novelty=DraftNovelty(judgment="uncertain"),
    taxonomy=ModelTaxonomyV1(
        subject_codes=(), event_family="other", change_state="unknown", assertion_status="unknown"
    ),
    confidence=0.0,
    reasoning="drafting failed",
)


__all__ = [
    "DEFAULT_DRAFT_CONCURRENCY",
    "DRAFTABLE_DIMENSIONS",
    "DRAFTER_ID",
    "DRAFT_SCHEMA",
    "TAXONOMY_BLIND_DRAFTER_ID",
    "TAXONOMY_DIMENSIONS",
    "ConfiguredDrafterLM",
    "DraftExplanation",
    "DraftedReview",
    "ReviewDraft",
    "ReviewDraftBatch",
    "ReviewDrafter",
    "RubricDraft",
    "TaxonomyBlindDrafter",
    "build_draft_batch",
    "build_drafter_lm",
    "merge_spend",
    "spend_by_model",
    "submission_payload",
    "taxonomy_dimensions",
]
