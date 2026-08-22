"""#143 review finding 5: the advisory-rejection repair instruction must actually reach the reflection model."""

from __future__ import annotations

from typing import Any

import dspy  # type: ignore[import-untyped]

from tracefold.news.agents.program_proposer import RulePackAwareProposer
from tracefold.news.agents.semantic_program import load_stable_program_artifact

_EXAMPLES = [{"Inputs": {}, "Generated Outputs": {}, "Feedback": "magnitude was wrong"}]


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


def test_proposer_reasks_when_the_advisory_bounds_reject_its_text() -> None:
    """The metric's repair instruction was real but unreachable.

    An advisory is rejected *before* any provider call, so nothing reaches `dspy.settings.trace`; GEPA's
    `make_reflective_dataset` then finds no instances for the component and skips the whole iteration in
    silence. The only place the code can still be delivered is here, while the model that wrote the text is
    still in the loop.
    """

    proposer = RulePackAwareProposer(load_stable_program_artifact())
    lm = _ScriptedReflectionLM(
        [
            "```\nAlways consult https://example.invalid/rules first.\n```",  # rejected: URL
            "```\nTreat a named production-capacity commitment as magnitude 2.\n```",  # accepted
        ]
    )
    with dspy.context(lm=lm):
        updated = proposer(
            candidate={"event_semantics": ""},
            reflective_dataset={"event_semantics": _EXAMPLES},
            components_to_update=["event_semantics"],
        )

    assert proposer.rejections == ["news_program_learned_strategy_unsafe"]
    assert len(lm.prompts) == 2, "the proposer did not ask again after the rejection"
    assert "news_program_learned_strategy_unsafe" in lm.prompts[1]
    assert updated["event_semantics"].startswith("Treat a named production-capacity commitment")


def test_proposer_drops_a_component_it_cannot_make_safe() -> None:
    """Leaving the component unchanged beats handing GEPA text the applier will refuse."""

    proposer = RulePackAwareProposer(load_stable_program_artifact())
    unsafe = "```\nSee https://example.invalid/x\n```"
    with dspy.context(lm=_ScriptedReflectionLM([unsafe, unsafe])):
        updated = proposer(
            candidate={"reader_card": ""},
            reflective_dataset={"reader_card": _EXAMPLES},
            components_to_update=["reader_card"],
        )
    assert updated == {}
    assert proposer.rejections == ["news_program_learned_strategy_unsafe"]


def test_proposer_passes_a_safe_proposal_through_without_a_second_call() -> None:
    proposer = RulePackAwareProposer(load_stable_program_artifact())
    lm = _ScriptedReflectionLM(["```\nPrefer the accepted magnitude for capacity commitments.\n```"])
    with dspy.context(lm=lm):
        updated = proposer(
            candidate={"event_semantics": ""},
            reflective_dataset={"event_semantics": _EXAMPLES},
            components_to_update=["event_semantics"],
        )
    assert len(lm.prompts) == 1
    assert proposer.rejections == []
    assert updated["event_semantics"].startswith("Prefer the accepted magnitude")
