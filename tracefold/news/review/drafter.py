"""Model-drafted `news_review_v6` rubrics, for a human to accept or reject (#117, #148).

Two facts set the whole shape of this module.

First, gold coverage is 0.1226 and *all* of it is `novelty` — no accepted review states a correct
`magnitude`, `direction` or `assets`, because `expected` only landed in #143 and nobody has used it. Without
gold, a failed dimension scores on "did anything change", so an optimizer can bank points by changing a value
to another wrong one. Stating the right answer is worth more than any amount of extra compute.

Second, `ReviewDesk.submit` writes an `acceptance` row unconditionally — there is no draft state. Anything
written through that path is accepted release evidence the instant it lands.

Therefore a draft is **not** a review and never touches `news_reviews`. This produces a file. A human reads it,
edits what is wrong, and submits the approved subset through the existing `review submit` — which stays the
one and only writer. The model's job is to turn "compose a judgment from scratch" into "confirm or reject one",
not to become an author of record.
"""

from __future__ import annotations

import importlib.metadata
from collections.abc import Mapping, Sequence
from typing import Any, Final, Literal, cast

import dspy  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field

from ..artifact_identity import canonical_sha
from ..program.contracts import (
    ReaderValue,
    TradeAffectedMarket,
    TradeChannel,
    TradeDevelopmentDelta,
    TradeImpactBreadth,
    TradeSurprise,
    TradeTradability,
)
from ..program.lm import StructuredOutputMode, structured_output_capability
from ..taxonomy import ModelTaxonomyV1, NewsTaxonomyV1, SourceAuthority


class ConfiguredDrafterLM(dspy.LM):  # type: ignore[misc]
    """Stock DSPy LM with the endpoint's explicit structured-output capability."""

    def __init__(self, model: str, *, structured_output: StructuredOutputMode, **kwargs: Any) -> None:
        self._structured_output = structured_output
        super().__init__(model, **kwargs)

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


DRAFTER_ID = "tracefold.news.review_drafter_v4"
# `Final` is what makes mypy infer the literal type rather than `str`, which is what the
# `Literal[...]` field below needs as its default.
DRAFT_SCHEMA: Final = "tracefold.news.review_draft_batch.v3"

_INSTRUCTION = """You are drafting a quality review of one already-published Chinese news card for a
crypto/US-equity trading desk. A human will accept or reject your draft; never assume it is final.

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

Do NOT judge why_support or why_value. Leave them out of `dimensions` entirely — the human reviewer writes
those. Measured against 25 human-reviewed Events you agree with a reviewer 76-88% of the time on the
dimensions above, but only 42-43% on those two, and 27 of 46 total false failures came from them alone.

should_push: must_push / should_push / should_hold / must_hold / uncertain — whether a trader needed this.
Reserve `must_*` for cases where the opposite decision would be a real failure (a security incident missed,
marketing pushed).

novelty: new_fact / progression / restatement, judged against the told ledger you are shown. Use
`restatement` only when a told entry carries the same fact, and then name that entry's event_id.

expected: ONLY for dimensions you marked fail, state the exact correct value — including every failed typed
trade-relevance dimension. Leave a field out when you are not confident.
This is the most valuable part of the draft: "wrong" without "and the answer is X" teaches nothing.

taxonomy: state the exact `news_taxonomy_v1` model-owned labels for every Event: at most three allowed IPTC
subject qcodes, event_family, change_state, and assertion_status. Also judge all five taxonomy dimensions;
source_authority is computed by code and appears in card_json, so copy that exact value rather than guessing.
`filing`, `rumor`, `whale`, and `noise` are not event families. Use other/unknown as honest abstentions.

confidence: 0.0-1.0, how sure you are a human would agree with this draft.
reasoning: one short sentence a reviewer can check quickly."""


class DraftAsset(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str = Field(min_length=1, max_length=32)
    role: Literal["primary", "mentioned"] = "primary"


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


class DraftNovelty(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    judgment: Literal["new_fact", "progression", "restatement", "uncertain"]
    duplicate_of: str = Field(default="", max_length=128)


# What the drafter is allowed to judge, measured rather than assumed. Agreement with a human reviewer over 25
# Events they both saw: direction 88%, factual_fidelity 84%, headline_fidelity 84%, magnitude 76%,
# asset_grounding 70% — against why_support 43% and why_value 42%. Those two also produced 27 of the 46
# "human passed it, the draft failed it" disagreements, so letting the model touch them would push a large
# number of failures a reviewer disagrees with into the corpus, where the optimizer would learn from them.
# They also carry no gold: "the correct Chinese sentence" is not a value a rubric can hold.
DRAFTABLE_DIMENSIONS = frozenset(
    {
        "factual_fidelity",
        "headline_fidelity",
        "asset_grounding",
        "direction",
        "magnitude",
        "timeliness",
        "trade_impact_breadth",
        "trade_tradability",
        "trade_surprise",
        "trade_development_delta",
        "trade_channels",
        "trade_affected_markets",
        "reader_value",
        "taxonomy_subject_codes",
        "taxonomy_event_family",
        "taxonomy_change_state",
        "taxonomy_source_authority",
        "taxonomy_assertion_status",
    }
)


class ReviewDraft(BaseModel):
    """What the model proposes. Shaped so a human edit turns it into an `EventRubricSubmission`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    should_push: Literal["must_push", "should_push", "should_hold", "must_hold", "uncertain"]
    dimensions: dict[str, Literal["pass", "fail", "not_applicable"]]
    novelty: DraftNovelty
    expected: DraftExpected | None = None
    taxonomy: ModelTaxonomyV1
    expected_correction: str = Field(default="", max_length=2_000)
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str = Field(default="", max_length=1_000)


class _ReviewDraftSignature(dspy.Signature):  # type: ignore[misc]
    evidence_json: str = dspy.InputField(desc="Bounded original evidence for the Event")
    card_json: str = dspy.InputField(desc="The verdict and card the system produced")
    told_json: str = dspy.InputField(desc="Cards already sent in the 4 h window, newest first")
    draft: ReviewDraft = dspy.OutputField(desc="Proposed rubric; a human decides")


class DraftedReview(BaseModel):
    """One draft plus everything a human needs to accept it through `review submit`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str
    task_version: str
    event_id: str
    headline_zh: str
    source_authority: SourceAuthority
    draft: ReviewDraft
    error: str | None = None


class ReviewDraftBatch(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_id: Literal["tracefold.news.review_draft_batch.v3"] = DRAFT_SCHEMA
    drafter: dict[str, Any]
    drafts: tuple[DraftedReview, ...]

    @property
    def batch_sha256(self) -> str:
        return canonical_sha(self.model_dump(mode="json"))


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
            "model": str(getattr(self._lm, "model", "") or ""),
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
            "authority": "proposal_only; a human accepts it through ReviewDesk.submit",
        }

    def draft(self, *, evidence_json: str, card_json: str, told_json: str) -> ReviewDraft | str:
        """Return a draft, or an error string. Never raises: one bad Event must not end the batch."""

        self.calls += 1
        try:
            with dspy.context(lm=self._lm, adapter=dspy.JSONAdapter(use_native_function_calling=False)):
                prediction = self._predict(
                    evidence_json=evidence_json,
                    card_json=card_json,
                    told_json=told_json,
                )
            draft = prediction.draft
            return draft if isinstance(draft, ReviewDraft) else ReviewDraft.model_validate(draft)
        except Exception as exc:
            self.failures += 1
            return f"{type(exc).__name__}: {str(exc)[:200]}"


def submission_payload(
    draft: ReviewDraft,
    *,
    source_authority: SourceAuthority = "unknown",
    draft_author: str = DRAFTER_ID,
) -> dict[str, Any]:
    """The `EventRubricSubmission` a human would send after accepting this draft, unchanged.

    Built here so the accept step never has to reshape model output by hand, and so the rubric's own
    validators — gold only on failed dimensions, evidence refs required for a fail — are what decide whether
    a draft is submittable at all.
    """

    # Every label the rubric accepts, `not_applicable` included. Filtering to pass/fail looks tidier and is
    # wrong: `should_push` of `must_push`/`should_push` *requires* a `timeliness` entry, and that entry is
    # `not_applicable` on most Events — dropping it makes the majority of drafts unsubmittable.
    # A model that answers anyway is silently dropped rather than trusted: the reviewer owns `why_*`.
    dimensions = {name: label for name, label in draft.dimensions.items() if name in DRAFTABLE_DIMENSIONS}
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
    taxonomy = NewsTaxonomyV1.issue(draft.taxonomy, source_authority=source_authority)
    payload: dict[str, Any] = {
        "kind": "event_rubric",
        "should_push": draft.should_push,
        "dimensions": dimensions,
        "novelty": novelty,
        "expected_correction": draft.expected_correction,
        "taxonomy": taxonomy.model_dump(mode="json"),
        "taxonomy_review": {
            "label_source": "model_draft",
            "draft_author": draft_author,
            "review_role": "primary",
            "draft_taxonomy": taxonomy.model_dump(mode="json"),
        },
    }
    if expected:
        payload["expected"] = expected
    if any(label == "fail" for label in dimensions.values()):
        # The rubric refuses a `fail` without one; the drafter cannot invent an operator's citation, so it
        # points at the two things it did read.
        payload["evidence_refs"] = ["source:leader:title", f"draft:{DRAFTER_ID}"]
    return payload


def build_draft_batch(
    drafter: ReviewDrafter,
    tasks: Sequence[Mapping[str, Any]],
) -> ReviewDraftBatch:
    """Draft one review per task. Each task carries the three rendered inputs plus its identity."""

    task_ids = [str(task["task_id"]) for task in tasks]
    if len(task_ids) != len(set(task_ids)):
        raise ValueError("news_review_drafter_duplicate_task")
    drafts: list[DraftedReview] = []
    for task in tasks:
        outcome = drafter.draft(
            evidence_json=str(task["evidence_json"]),
            card_json=str(task["card_json"]),
            told_json=str(task.get("told_json") or "[]"),
        )
        drafts.append(
            DraftedReview(
                task_id=str(task["task_id"]),
                task_version=str(task["task_version"]),
                event_id=str(task["event_id"]),
                headline_zh=str(task.get("headline_zh") or ""),
                source_authority=cast(SourceAuthority, str(task.get("source_authority") or "unknown")),
                draft=outcome if isinstance(outcome, ReviewDraft) else _EMPTY_DRAFT,
                error=None if isinstance(outcome, ReviewDraft) else outcome,
            )
        )
    return ReviewDraftBatch(
        drafter={**drafter.identity, "calls": drafter.calls, "failures": drafter.failures},
        drafts=tuple(drafts),
    )


_EMPTY_DRAFT = ReviewDraft(
    should_push="uncertain",
    dimensions={},
    novelty=DraftNovelty(judgment="uncertain"),
    taxonomy=ModelTaxonomyV1(
        subject_codes=(), event_family="other", change_state="unknown", assertion_status="unknown"
    ),
    confidence=0.0,
    reasoning="drafting failed",
)


__all__ = [
    "DRAFTABLE_DIMENSIONS",
    "DRAFTER_ID",
    "DRAFT_SCHEMA",
    "ConfiguredDrafterLM",
    "DraftedReview",
    "ReviewDraft",
    "ReviewDraftBatch",
    "ReviewDrafter",
    "build_draft_batch",
    "build_drafter_lm",
    "submission_payload",
]
