from dataclasses import replace

from tracefold.trading.engine.evaluation import EvaluationRun, EvaluatorSpec


def spec() -> EvaluatorSpec:
    return EvaluatorSpec("a" * 64, "fixture", "revision-1", "case_view_v2", "forecast_v1", 2000)


def test_same_prompt_different_model_or_contract_has_distinct_evaluation_identity() -> None:
    original = spec()
    changed = (
        replace(original, model_name="other"),
        replace(original, model_revision="revision-2"),
        replace(original, input_contract="case_view_v3"),
        replace(original, max_output_tokens=3000),
    )
    assert all(value.evaluator_id != original.evaluator_id for value in changed)
    assert EvaluationRun.online(original) == EvaluationRun.online(spec())


def test_dataset_policy_and_explicit_sampling_tag_define_resumable_replay() -> None:
    args = {
        "case_ids": ["b", "a"],
        "since_ms": 1,
        "until_ms": 100,
        "policy_config": {"min_expected_r": "0"},
        "mode": "inference",
    }
    original = EvaluationRun.replay(spec(), **args)
    assert original == EvaluationRun.replay(spec(), **{**args, "case_ids": ["a", "b"]})
    assert original.run_id != EvaluationRun.replay(spec(), **{**args, "case_ids": ["a"]}).run_id
    assert original.run_id != EvaluationRun.replay(spec(), **{**args, "tag": "resample-2"}).run_id
    changed = EvaluationRun.replay(spec(), **{**args, "policy_config": {"min_expected_r": "0.1"}})
    assert changed.evaluator_id == original.evaluator_id and changed.run_id != original.run_id
