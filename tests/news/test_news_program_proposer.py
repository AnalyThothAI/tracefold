"""#143 review finding 5: the rejection repair instruction must actually reach the reflection model."""

from __future__ import annotations

from typing import Any

import dspy  # type: ignore[import-untyped]

from tracefold.news.learning.optimizer import InstructionProposer
from tracefold.news.program.artifact import load_stable_program_artifact

_EXAMPLES = [{"Inputs": {}, "Generated Outputs": {}, "Feedback": "magnitude was wrong"}]
# The component text GEPA carries *is* the whole instruction now, so the "current" side of a proposal is
# the shipped seed rather than an empty advisory slot.
_CURRENT = {name: load_stable_program_artifact().instruction_for(name) for name in ("event_semantics", "reader_card")}


class _ScriptedReflectionLM(dspy.BaseLM):  # type: ignore[misc]
    def __init__(self, replies: list[str]) -> None:
        super().__init__(model="scripted/reflection")
        self._replies = iter(replies)
        self.prompts: list[str] = []

    def __call__(self, prompt: Any = None, messages: Any = None, **kwargs: Any) -> list[str]:
        self.prompts.append(str(prompt if isinstance(prompt, str) else messages))
        try:
            return [next(self._replies)]
        except StopIteration:
            return [(self.prompts and "```\n(exhausted)\n```") or ""]


def test_proposer_reasks_when_the_instruction_bounds_reject_its_text() -> None:
    """The metric's repair instruction was real but unreachable.

    A proposal is rejected *before* any provider call, so nothing reaches `dspy.settings.trace`; GEPA's
    `make_reflective_dataset` then finds no instances for the component and skips the whole iteration in
    silence. The only place the code can still be delivered is here, while the model that wrote the text is
    still in the loop.
    """

    proposer = InstructionProposer(load_stable_program_artifact())
    lm = _ScriptedReflectionLM(
        [
            "```\nAlways consult https://example.invalid/rules first.\n```",  # rejected: URL
            "```\nTreat a named production-capacity commitment as magnitude 2.\n```",  # accepted
        ]
    )
    with dspy.context(lm=lm):
        updated = proposer(
            candidate={"event_semantics": _CURRENT["event_semantics"]},
            reflective_dataset={"event_semantics": _EXAMPLES},
            components_to_update=["event_semantics"],
        )

    assert proposer.rejections == ["news_program_instruction_unsafe"]
    assert len(lm.prompts) == 2, "the proposer did not ask again after the rejection"
    assert "news_program_instruction_unsafe" in lm.prompts[1]
    assert updated["event_semantics"].startswith("Treat a named production-capacity commitment")


def test_proposer_drops_a_component_it_cannot_make_safe() -> None:
    """Leaving the component unchanged beats handing GEPA text the applier will refuse."""

    proposer = InstructionProposer(load_stable_program_artifact())
    unsafe = "```\nSee https://example.invalid/x\n```"
    with dspy.context(lm=_ScriptedReflectionLM([unsafe, unsafe])):
        updated = proposer(
            candidate={"reader_card": _CURRENT["reader_card"]},
            reflective_dataset={"reader_card": _EXAMPLES},
            components_to_update=["reader_card"],
        )
    assert updated == {}
    assert proposer.rejections == ["news_program_instruction_unsafe"]


def test_proposer_passes_a_safe_proposal_through_without_a_second_call() -> None:
    proposer = InstructionProposer(load_stable_program_artifact())
    lm = _ScriptedReflectionLM(["```\nPrefer the accepted magnitude for capacity commitments.\n```"])
    with dspy.context(lm=lm):
        updated = proposer(
            candidate={"event_semantics": _CURRENT["event_semantics"]},
            reflective_dataset={"event_semantics": _EXAMPLES},
            components_to_update=["event_semantics"],
        )
    assert len(lm.prompts) == 1
    assert proposer.rejections == []
    assert updated["event_semantics"].startswith("Prefer the accepted magnitude")


def test_the_brief_tells_the_writer_it_is_replacing_the_whole_instruction() -> None:
    """#306 Phase 2 inverted what the brief is for.

    `RulePackAwareProposer` pasted the rendered read-only prompt in so the model would not duplicate it.
    There is no surrounding prompt left, so the brief now states the opposite responsibility: everything
    you drop is gone, and the calibrations a human review put there are not yours to shed.
    """

    proposer = InstructionProposer(load_stable_program_artifact())
    brief = proposer.context_for("event_semantics")

    assert "replaces the whole instruction" in brief
    assert "anything you drop is gone from the prompt" in brief
    assert "a shorter instruction that lost a" in brief and "calibration is a regression" in brief
    # And it no longer carries a copy of the prompt: that is the component text itself.
    assert "RULEPACK" not in brief and "LEARNEDSTRATEGY" not in brief
