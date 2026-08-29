"""#143 review finding 5: the rejection repair instruction must actually reach the reflection model."""

from __future__ import annotations

from typing import Any

from tracefold.news.learning.optimizer import InstructionGrowthBudget, InstructionProposer
from tracefold.news.program.artifact import load_stable_program_artifact

_EXAMPLES = [{"Inputs": {}, "Generated Outputs": {}, "Feedback": "magnitude was wrong"}]
# The component text GEPA carries *is* the whole instruction now, so the "current" side of a proposal is
# the shipped seed rather than an empty advisory slot.
_CURRENT = {name: load_stable_program_artifact().instruction_for(name) for name in ("event_semantics", "reader_card")}


class _ScriptedReflectionLM:
    """The reflection role's whole contract: one callable, one string in, one string out."""

    def __init__(self, replies: list[str]) -> None:
        self._replies = iter(replies)
        self.prompts: list[str] = []

    def __call__(self, prompt: Any) -> str:
        self.prompts.append(str(prompt))
        try:
            return next(self._replies)
        except StopIteration:
            return "```\n(exhausted)\n```"


def test_proposer_reasks_when_the_instruction_bounds_reject_its_text() -> None:
    """The metric's repair instruction was real but unreachable.

    A proposal is rejected *before* any provider call, so the next round's reflective dataset carries the
    previous candidate's outputs and the metric's repair instruction never reaches anyone. The only place
    the code can still be delivered is here, while the model that wrote the text is still in the loop.
    """

    lm = _ScriptedReflectionLM(
        [
            # Rejected for exceeding the instruction budget. #319 removed the marker blacklist that used
            # to trigger this path; the budget is a surviving bound and the re-ask behaviour it proves —
            # deliver the error code while the model that wrote the text is still in the loop — is the
            # business correctness this test is actually about.
            f"```\n{'Restate the rule at length. ' * 1400}\n```",
            "```\nTreat a named production-capacity commitment as magnitude 2.\n```",  # accepted
        ]
    )
    proposer = InstructionProposer(reflection_lm=lm)
    updated = proposer(
        candidate={"event_semantics": _CURRENT["event_semantics"]},
        reflective_dataset={"event_semantics": _EXAMPLES},
        components_to_update=["event_semantics"],
    )

    assert proposer.rejections == ["news_program_instruction_too_large"]
    assert len(lm.prompts) == 2, "the proposer did not ask again after the rejection"
    assert "news_program_instruction_too_large" in lm.prompts[1]
    assert updated["event_semantics"].startswith("Treat a named production-capacity commitment")


def test_proposer_drops_a_component_it_cannot_make_safe() -> None:
    """Leaving the component unchanged beats handing GEPA text the applier will refuse."""

    lm = _ScriptedReflectionLM([oversized := f"```\n{'Restate the rule at length. ' * 1400}\n```", oversized])
    proposer = InstructionProposer(reflection_lm=lm)
    updated = proposer(
        candidate={"reader_card": _CURRENT["reader_card"]},
        reflective_dataset={"reader_card": _EXAMPLES},
        components_to_update=["reader_card"],
    )
    assert updated == {}
    assert proposer.rejections == ["news_program_instruction_too_large"]


def test_proposer_passes_a_safe_proposal_through_without_a_second_call() -> None:
    lm = _ScriptedReflectionLM(["```\nPrefer the accepted magnitude for capacity commitments.\n```"])
    proposer = InstructionProposer(reflection_lm=lm)
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

    proposer = InstructionProposer(reflection_lm=_ScriptedReflectionLM([]))
    brief = proposer.context_for("event_semantics")

    assert "replaces the whole instruction" in brief
    assert "anything you drop is gone from the prompt" in brief
    assert "a shorter instruction that lost a" in brief and "calibration is a regression" in brief
    # And it no longer carries a copy of the prompt: that is the component text itself.
    assert "RULEPACK" not in brief and "LEARNEDSTRATEGY" not in brief


def test_proposer_prices_instruction_growth_during_selection() -> None:
    """#334: the offline gate's token constraint, delivered while the writer is still in the loop.

    #199's first ADVANCE scored +2.60 and died at the release gate for +4.7KB of instruction — nothing in
    GEPA's world had said bytes cost anything. The budget makes the first oversized proposal come back as
    a re-ask that names the numbers, so the reflection model compresses instead of the run wasting four
    hours to learn the same thing.
    """

    lm = _ScriptedReflectionLM(
        [
            f"```\n{'Add a well-meant but long clarification. ' * 40}\n```",  # safe, but over seed+budget
            "```\nMerge the two overlapping magnitude rules into one sentence.\n```",  # compressed
        ]
    )
    proposer = InstructionProposer(
        reflection_lm=lm,
        budget=InstructionGrowthBudget.from_seeds({"event_semantics": "Seed rule. " * 30}, max_growth_tokens=50),
    )
    updated = proposer(
        candidate={"event_semantics": _CURRENT["event_semantics"]},
        reflective_dataset={"event_semantics": _EXAMPLES},
        components_to_update=["event_semantics"],
    )

    assert proposer.rejections == ["news_program_instruction_growth_budget"]
    assert len(lm.prompts) == 2
    assert "news_program_instruction_growth_budget" in lm.prompts[1]
    assert "per-observation tokens" in lm.prompts[1], "the re-ask must name the release gate's reason, not just a code"
    assert updated["event_semantics"].startswith("Merge the two overlapping magnitude rules")


def test_growth_budget_is_anchored_to_the_seed_not_the_current_candidate() -> None:
    """An anchor that moved with each accepted round would let the allowance ratchet upward."""

    seed = "Seed rule. " * 30  # ~90 estimated tokens
    grown_candidate = "Previously accepted growth. " * 80  # far past seed + 50 already
    shorter_than_candidate = "```\n" + "Still too long for the seed budget. " * 40 + "\n```"

    lm = _ScriptedReflectionLM([shorter_than_candidate, shorter_than_candidate])
    proposer = InstructionProposer(
        reflection_lm=lm,
        budget=InstructionGrowthBudget.from_seeds({"event_semantics": seed}, max_growth_tokens=50),
    )
    updated = proposer(
        candidate={"event_semantics": grown_candidate},
        reflective_dataset={"event_semantics": _EXAMPLES},
        components_to_update=["event_semantics"],
    )

    # Shorter than the candidate it replaces, yet rejected twice: the budget reads the seed, not the drift.
    assert updated == {}
    assert proposer.rejections == ["news_program_instruction_growth_budget"]


def test_without_seed_instructions_only_the_safety_bounds_apply() -> None:
    """Direct constructions (tests, probes) opt in to the budget; `run_gepa` always supplies the seeds."""

    long_but_valid = "```\n" + "A long yet lawful instruction. " * 200 + "\n```"
    lm = _ScriptedReflectionLM([long_but_valid])
    proposer = InstructionProposer(reflection_lm=lm)
    updated = proposer(
        candidate={"event_semantics": _CURRENT["event_semantics"]},
        reflective_dataset={"event_semantics": _EXAMPLES},
        components_to_update=["event_semantics"],
    )

    assert proposer.rejections == []
    assert updated["event_semantics"].startswith("A long yet lawful instruction.")


def test_the_headroom_is_shared_across_components_like_the_gate_charges_it() -> None:
    """The gate charges one ~10% window per observation, and both instructions ride it.

    A per-component allowance would admit a candidate whose two components each grew "within budget" while
    their sum blows the gate — which is exactly what a round-robin run or a merge produces. The envelope is
    therefore charged over the whole candidate: growth already accepted into one component spends headroom
    the other can no longer use.
    """

    seeds = {"event_semantics": "Seed rule. " * 30, "reader_card": "Card rule. " * 30}
    budget = InstructionGrowthBudget.from_seeds(seeds, max_growth_tokens=50)
    modest = "```\n" + "A modest addition. " * 20 + "\n```"
    lm = _ScriptedReflectionLM([modest, modest])
    proposer = InstructionProposer(reflection_lm=lm, budget=budget)

    updated = proposer(
        candidate={
            "event_semantics": seeds["event_semantics"],
            "reader_card": seeds["reader_card"] + "Extra colour. " * 40,
        },
        reflective_dataset={"event_semantics": _EXAMPLES},
        components_to_update=["event_semantics"],
    )

    assert updated == {}
    assert proposer.rejections == ["news_program_instruction_growth_budget"]
    # The identical proposal fits when the other component has not eaten the shared headroom.
    lm2 = _ScriptedReflectionLM([modest])
    proposer2 = InstructionProposer(reflection_lm=lm2, budget=budget)
    accepted = proposer2(
        candidate=dict(seeds),
        reflective_dataset={"event_semantics": _EXAMPLES},
        components_to_update=["event_semantics"],
    )
    assert accepted["event_semantics"].startswith("A modest addition.")
