from __future__ import annotations

from tracefold.news.agents.quality_baseline import (
    EVENT_SEMANTICS_INSTRUCTION,
    EXPERT_BASELINE_COVERAGE,
    READER_CARD_INSTRUCTION,
    validate_expert_baseline_coverage,
)
from tracefold.news.agents.semantic_program import PROGRAM_INSTRUCTION_MAX_BYTES, load_stable_program_artifact


def test_stable_artifact_carries_the_reviewed_expert_baseline() -> None:
    artifact = load_stable_program_artifact()

    assert artifact.event_semantics.instruction == EVENT_SEMANTICS_INSTRUCTION
    assert artifact.reader_card.instruction == READER_CARD_INSTRUCTION
    assert set(EXPERT_BASELINE_COVERAGE) == {"event_semantics", "reader_card"}
    assert set(EXPERT_BASELINE_COVERAGE["event_semantics"]) == {
        "untrusted_evidence",
        "asset_grounding",
        "raw_first_line",
        "magnitude_zero",
        "magnitude_one",
        "magnitude_two",
        "magnitude_three",
        "own_product",
        "milestone",
        "direction",
        "actionable",
        "decision_owner",
        "audience",
        "price_a",
        "price_b",
        "price_b_positive",
        "price_c",
        "price_d",
        "price_d_positive",
        "price_e",
        "price_negative",
        "law_firm",
        "meme",
        "competition",
        "airdrop",
        "scheduled_macro",
        "new_fact",
        "progression",
        "restatement",
        "direction_reversal",
        "direction_reversal_example",
    }
    assert set(EXPERT_BASELINE_COVERAGE["reader_card"]) == {
        "untrusted_evidence",
        "faithful_chinese",
        "chinese_unchanged",
        "numbers",
        "critical_clause",
        "lost_clause_example",
        "lost_number_example",
        "why_mechanism",
        "direction_agreement",
        "banned_filler",
        "no_meta_opening",
        "no_self_description",
    }

    validate_expert_baseline_coverage()
    assert "title_zh" not in READER_CARD_INSTRUCTION
    assert len(EVENT_SEMANTICS_INSTRUCTION.encode("utf-8")) <= PROGRAM_INSTRUCTION_MAX_BYTES
    assert len(READER_CARD_INSTRUCTION.encode("utf-8")) <= PROGRAM_INSTRUCTION_MAX_BYTES


def test_expert_baseline_coverage_includes_required_cases() -> None:
    event_cases = set(EXPERT_BASELINE_COVERAGE["event_semantics"])
    assert {"price_b_positive", "price_d_positive", "direction_reversal_example"} <= event_cases
