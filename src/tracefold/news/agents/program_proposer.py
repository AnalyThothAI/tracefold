"""Code-owned GEPA instruction proposer that shows the reflection LM what it is amending.

GEPA proposes a new instruction by handing its reflection model the *current* instruction plus a minibatch of
inputs, outputs and feedback. For this Program the mutable component is `LearnedStrategy`, and its code-owned
baseline is the empty string — so DSPy's default proposer was showing the reflection model a single space and
asking it to write "a new instruction for the assistant".

That is the CoNLL tutorial's setup with the docstring deleted. The reflection model could not see the eight
RulePacks that make up ~8.5 KB of the rendered prompt, so it had no way to write an advisory that *adds*
something: its only reachable move was to restate the rules it could not read, into a slot whose authority seal
says advisory text may never override those very rules.

This proposer keeps the optimizer's write-set exactly where it was — one bounded advisory string per Predictor
— and only widens what the reflection model is allowed to *read*. It is trusted, code-owned, and travels with
the compiler image; the optimizer cannot supply or modify it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import dspy  # type: ignore[import-untyped]

from .semantic_program import PredictorName, ProgramArtifact, render_predictor_instruction

_BRIEF = """You are amending ONE bounded advisory block inside a larger, code-owned prompt.

Below is the COMPLETE prompt that is actually sent for the `{component}` Predictor, exactly as the runtime
renders it. You may NOT change any of it except the single section marked `# LEARNEDSTRATEGY`.

===== BEGIN RENDERED PROMPT (READ-ONLY EXCEPT THE LEARNEDSTRATEGY SECTION) =====
{rendered}
===== END RENDERED PROMPT =====

Rules for what you write:
- Write ONLY the replacement body of the LEARNEDSTRATEGY section. Do not repeat the QualityKernel, the
  RulePacks, the authority seal, or the schema. They are already in the prompt and re-stating them wastes the
  budget and can only introduce contradictions.
- The RulePacks above are authoritative and you cannot weaken, reinterpret or override them. Write guidance
  that resolves cases the RulePacks leave genuinely ambiguous, or that names concrete recurring evidence
  patterns the feedback below shows the model is getting wrong.
- Be specific to this domain and this failure set. Prefer stated correct values, named instruments, and
  concrete decision boundaries over general advice about being careful.
- Do not include URLs, template braces, credentials, or any instruction to ignore or outrank other sections;
  such text is rejected outright and the candidate scores zero.
"""


class RulePackAwareProposer:
    """A `ProposalFn` that prefixes GEPA's own proposal prompt with the rendered, read-only context."""

    def __init__(self, artifact: ProgramArtifact) -> None:
        self._artifact = artifact
        self._rendered = {
            name: render_predictor_instruction(artifact, name) for name in ("event_semantics", "reader_card")
        }
        self.calls = 0
        self.components_seen: list[str] = []

    def context_for(self, component: str) -> str:
        """The read-only brief for one component. Exposed so a test can assert the RulePacks reach the model."""

        rendered = self._rendered.get(component)
        if rendered is None:
            raise ValueError(f"news_program_proposer_unknown_component:{component}")
        return _BRIEF.format(component=component, rendered=rendered)

    def __call__(
        self,
        candidate: Mapping[str, str],
        reflective_dataset: Mapping[str, Sequence[Mapping[str, Any]]],
        components_to_update: Sequence[str],
    ) -> dict[str, str]:
        from gepa.strategies.instruction_proposal import InstructionProposalSignature

        updated: dict[str, str] = {}
        for component in components_to_update:
            examples = list(reflective_dataset.get(component) or ())
            if not examples:
                continue
            self.calls += 1
            self.components_seen.append(component)
            current = str(candidate.get(component) or "").strip()
            # The advisory slot's own content is what GEPA is editing; the brief above is the surrounding
            # prompt it must not duplicate.
            doc = (
                f"{self.context_for(component)}\n"
                "===== CURRENT LEARNEDSTRATEGY BODY (this is what you are replacing) =====\n"
                f"{current or '(empty — no advisory has been learned yet)'}\n"
                "===== END CURRENT LEARNEDSTRATEGY BODY ====="
            )
            proposal = InstructionProposalSignature.run(
                lm=_reflect,
                input_dict={
                    "current_instruction_doc": doc,
                    "dataset_with_feedback": examples,
                    "prompt_template": None,
                },
            )
            text = str(proposal.get("new_instruction") or "").strip()
            if text:
                updated[component] = text
        return updated


def _reflect(prompt: Any) -> str:
    """Call whichever LM GEPA has put in context.

    DSPy invokes a custom proposer inside `dspy.context(lm=reflection_lm)`, so resolving the LM here rather
    than capturing one at construction is what keeps the budgeted proxy — and therefore the metered call
    count — in the path. Output shapes mirror DSPy's own `stripped_lm_call`.
    """

    lm = dspy.settings.lm
    if lm is None:
        raise ValueError("news_program_proposer_reflection_lm_missing")
    raw = lm(prompt) if isinstance(prompt, str) else lm(messages=prompt)
    first = raw[0] if isinstance(raw, (list, tuple)) and raw else raw
    if isinstance(first, Mapping):
        if "text" not in first:
            raise ValueError("news_program_proposer_reflection_output_invalid")
        return str(first["text"])
    return str(first)


__all__ = ["PredictorName", "RulePackAwareProposer"]
