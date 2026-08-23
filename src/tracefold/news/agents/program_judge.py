"""Semantic equivalence for the metric's free-text retention anchors (#148).

`_component` keeps a reviewer's `pass` only when the candidate reproduces the accepted value. For an enum like
`magnitude` that is exactly right. For `headline_zh` and `why_zh` — whole Chinese sentences — it is wrong:
re-wording the same fact scores zero, so ReaderCard cannot win the anchor no matter how good the copy is.

Measured on the 162 accepted reviews, that single rule is worth ~13 points:

    candidate == production, byte-identical      0.9153
    wording differs, every card anchor lost      0.7825
    semantics enums drift too                    0.4663

The live baseline (0.587) sits between the last two. Card dimensions carry 15% of the weight, so GEPA was
optimizing against a component it could not move. This module is the RAG tutorial's `SemanticF1` idea applied
to exactly that: a small code-owned judge that answers "did the candidate keep what the reviewer accepted?"

Deliberately narrow:

* Only free-text dimensions consult it. `magnitude`, `direction`, `asset_grounding` and `novelty` stay on exact
  comparison — they are supposed to be exact, and a judge there would only add noise.
* It is asked only when the literal comparison already failed. Identical text needs no model call, so
  `--mode recorded` (where candidate *is* production) spends nothing and its golden 0.896373 is untouched.
* It belongs to the metric, not the Program: it never renders into a prompt, never enters the QualityKernel,
  and cannot change `program_sha256`.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from typing import Any, Literal

import dspy  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field

from ..artifact_identity import canonical_json, canonical_sha

JUDGE_ID = "tracefold.news.card_equivalence_judge_v1"

_INSTRUCTION = """You are checking whether a rewritten Chinese news card preserved what a human reviewer had
already accepted about the original. You are NOT judging which card is better written.

The reviewer read the ACCEPTED card and approved it. The CANDIDATE is a different attempt at the same event.
For each question answer only whether the candidate still carries the same substance.

- headline_equivalent: does the candidate headline state the same event, with the same subject, the same
  direction of action, and every decision-relevant number the accepted headline carried (amounts, percentages,
  price levels, deadlines, counts)? Different wording, word order or phrasing is fine. Dropping or changing a
  number, changing the subject, or losing the clause that says what happened is NOT equivalent.
- why_equivalent: does the candidate's one-sentence explanation give the same concrete mechanism — who is
  exposed and what changes for them? A different explanation of the same mechanism is equivalent. A different
  mechanism, a vaguer restatement of the headline, or an added consequence the accepted text did not claim is
  NOT equivalent.
- facts_preserved: does the candidate contradict the accepted card on any fact — a number, an entity, a
  direction, a causal link? You are also given each side's structured judgment (magnitude, direction, assets,
  event type). A candidate whose structured judgment contradicts the accepted one — the opposite direction, a
  different primary instrument — is NOT preserving the facts, even when the two texts read the same.

Judge only what you are given. Do not use outside knowledge about the event."""


class CardEquivalence(BaseModel):
    """Three independent answers, because the metric scores three separate dimensions."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    headline_equivalent: bool = Field(description="Same event, subject, action and decision-relevant numbers")
    why_equivalent: bool = Field(description="Same concrete mechanism and exposure")
    facts_preserved: bool = Field(description="Nothing in the candidate contradicts the accepted card")


class _CardEquivalenceSignature(dspy.Signature):  # type: ignore[misc]
    accepted_headline_zh: str = dspy.InputField(desc="Headline the reviewer accepted")
    accepted_why_zh: str = dspy.InputField(desc="Explanation the reviewer accepted")
    accepted_semantics_json: str = dspy.InputField(desc="Accepted structured judgment")
    candidate_headline_zh: str = dspy.InputField(desc="Headline the candidate produced")
    candidate_why_zh: str = dspy.InputField(desc="Explanation the candidate produced")
    candidate_semantics_json: str = dspy.InputField(desc="Candidate structured judgment")
    verdict: CardEquivalence = dspy.OutputField(desc="Per-dimension equivalence, no prose")


# `factual_fidelity` is a judgment about the whole card, so text alone cannot answer it: a candidate can copy
# both sentences verbatim and still flip `direction`. These fields travel with the text for that reason.
_SEMANTIC_FIELDS = ("event_type", "magnitude", "direction", "actionable", "scope", "assets")


def _semantics(verdict: Mapping[str, Any]) -> str:
    return canonical_json({name: verdict.get(name) for name in _SEMANTIC_FIELDS})


_IDENTICAL = CardEquivalence(headline_equivalent=True, why_equivalent=True, facts_preserved=True)
# What a judge failure means. Losing the anchor is the pre-#148 behaviour, so a judge that cannot answer
# degrades to the old, stricter score rather than silently handing out points.
_UNAVAILABLE = CardEquivalence(headline_equivalent=False, why_equivalent=False, facts_preserved=False)


class CardEquivalenceJudge:
    """One bounded model call per (accepted, candidate) card pair, memoized for the run."""

    def __init__(self, lm: dspy.LM, *, max_tokens: int = 8_192) -> None:
        self.lm = lm
        self._predict = dspy.Predict(
            _CardEquivalenceSignature.with_instructions(_INSTRUCTION),
            temperature=0,
            max_tokens=max_tokens,
        )
        self._cache: dict[str, CardEquivalence] = {}
        # `run_baseline` exposes `num_threads`; without this the counters written into the receipt under-count
        # and two threads on the same pair each pay for a provider call.
        self._lock = threading.Lock()
        self.calls = 0
        self.failures = 0

    @property
    def identity(self) -> dict[str, Any]:
        """Pinned into the metric receipt: two runs judged by different models are not comparable."""

        return {
            "judge_id": JUDGE_ID,
            "model": str(getattr(self.lm, "model", "") or ""),
            "instruction_sha256": canonical_sha(_INSTRUCTION),
            "signature_sha256": canonical_sha(sorted(_CardEquivalenceSignature.fields)),
            "output_schema_sha256": canonical_sha(CardEquivalence.model_json_schema()),
        }

    @property
    def stats(self) -> dict[str, int]:
        with self._lock:
            return {"calls": self.calls, "cache_entries": len(self._cache), "failures": self.failures}

    def equivalence(self, accepted: Mapping[str, Any], candidate: Mapping[str, Any]) -> CardEquivalence:
        accepted_headline = str(accepted.get("headline_zh") or "")
        accepted_why = str(accepted.get("why_zh") or "")
        candidate_headline = str(candidate.get("headline_zh") or "")
        candidate_why = str(candidate.get("why_zh") or "")
        accepted_semantics = _semantics(accepted)
        candidate_semantics = _semantics(candidate)
        # The short-circuit needs the structured judgment too. On text alone, a candidate that copied both
        # sentences and flipped `direction` would be handed `facts_preserved=True` for free.
        if (
            accepted_headline == candidate_headline
            and accepted_why == candidate_why
            and accepted_semantics == candidate_semantics
        ):
            return _IDENTICAL

        key = canonical_sha(
            [
                accepted_headline,
                accepted_why,
                accepted_semantics,
                candidate_headline,
                candidate_why,
                candidate_semantics,
            ]
        )
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                return cached
            self.calls += 1
        try:
            # Pin the adapter. Under DSPy's default the judge's structured reply fails the chat-format parse
            # and is silently retried as JSON — two provider calls for every one verdict, on a metric that runs
            # once per case per candidate.
            with dspy.context(lm=self.lm, adapter=dspy.JSONAdapter(use_native_function_calling=False)):
                prediction = self._predict(
                    accepted_headline_zh=accepted_headline,
                    accepted_why_zh=accepted_why,
                    accepted_semantics_json=accepted_semantics,
                    candidate_headline_zh=candidate_headline,
                    candidate_why_zh=candidate_why,
                    candidate_semantics_json=candidate_semantics,
                )
            verdict = prediction.verdict
            result = verdict if isinstance(verdict, CardEquivalence) else CardEquivalence.model_validate(verdict)
        except Exception:
            # Not cached: a transient provider failure must not pin this pair to "not equivalent" for the
            # rest of the run.
            with self._lock:
                self.failures += 1
            return _UNAVAILABLE
        with self._lock:
            self._cache[key] = result
        return result

    def retains(
        self,
        dimension: Literal["headline_fidelity", "why_support", "why_value", "factual_fidelity"],
        accepted: Mapping[str, Any],
        candidate: Mapping[str, Any],
    ) -> bool:
        """Whether the candidate keeps the reviewer's `pass` on one free-text dimension."""

        verdict = self.equivalence(accepted, candidate)
        if dimension == "headline_fidelity":
            return verdict.headline_equivalent
        if dimension in {"why_support", "why_value"}:
            return verdict.why_equivalent
        return verdict.facts_preserved


__all__ = ["JUDGE_ID", "CardEquivalence", "CardEquivalenceJudge"]
