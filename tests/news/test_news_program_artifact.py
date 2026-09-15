"""The native `NewsProgramStateV1` image: what loads, what round-trips, and what is refused (#651)."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from tracefold.news.program.artifact import (
    DSPY_STATE_VERSION,
    NewsProgramStateV1,
    _load_program_registry,
    build_code_owned_program_state,
    decode_program_state,
    encode_program_state,
    load_program_state,
    load_stable_program_state,
    predictor_document,
    read_program_state_document,
    write_program_candidate_state,
)
from tracefold.news.program.module import NativeNewsProgram
from tracefold.news.program.runtime import PREDICTOR_NAMES


def _candidate(
    *,
    event_suffix: str = "\nCandidate event rule.",
    taxonomy_suffix: str = "",
    card_suffix: str = "",
) -> NewsProgramStateV1:
    stable = load_stable_program_state()
    return NewsProgramStateV1.from_instructions(
        {
            "event_semantics": stable.instruction_for("event_semantics") + event_suffix,
            "taxonomy": stable.instruction_for("taxonomy") + taxonomy_suffix,
            "reader_card": stable.instruction_for("reader_card") + card_suffix,
        }
    )


def _mutated(**updates: Any) -> dict[str, Any]:
    raw = load_stable_program_state().model_dump(mode="json")
    raw.update(updates)
    return raw


def test_program_identity_is_the_schema_the_dspy_version_and_the_native_state() -> None:
    stable = load_stable_program_state()

    assert set(stable.model_dump()) == {
        "schema_version",
        "program_sha256",
        "dspy_version",
        "predictors",
        "state",
    }
    assert stable.schema_version == "news_program_state_v1"
    assert stable.dspy_version == DSPY_STATE_VERSION
    assert tuple(stable.predictors) == PREDICTOR_NAMES
    assert stable.program_sha256 == stable.computed_sha256()
    assert _candidate().program_sha256 != stable.program_sha256
    assert _candidate(event_suffix="", taxonomy_suffix="\nCandidate taxonomy rule.").program_sha256 != (
        stable.program_sha256
    )
    assert _candidate(event_suffix="", card_suffix="\nCandidate card rule.").program_sha256 != stable.program_sha256


def test_the_packaged_stable_image_is_the_seed_with_no_demos_and_one_registry_entry() -> None:
    registry = _load_program_registry()
    stable = load_stable_program_state()

    assert registry["images"] == (registry["stable"],)
    assert registry["stable"] == stable.program_sha256
    assert stable == build_code_owned_program_state()
    for predictor in PREDICTOR_NAMES:
        assert stable.demos_for(predictor) == ()
        assert stable.state[predictor]["lm"] is None


def test_state_round_trips_through_real_dspy_save_and_load() -> None:
    """Save -> load -> `named_predictors()`: same instructions, same demos, same Signature, no LM."""

    demo = {"evidence_json": "<evidence>", "taxonomy": {"subject_codes": []}}
    stable = load_stable_program_state()
    with_demo = stable.with_predictor_document(
        "taxonomy",
        predictor_document("taxonomy", instruction=stable.instruction_for("taxonomy"), demos=[demo]),
    )

    program = NativeNewsProgram(with_demo)
    loaded = dict(program.named_predictors())

    assert tuple(loaded) == PREDICTOR_NAMES
    for predictor in PREDICTOR_NAMES:
        predict = loaded[predictor]
        assert str(predict.signature.instructions) == with_demo.instruction_for(predictor)
        assert tuple(dict(entry) for entry in predict.demos) == with_demo.demos_for(predictor)
        # Field names, types and order are the code Signature's; only instructions and demos are state.
        assert list(predict.signature.fields) == list(
            NativeNewsProgram(stable).named_predictors()[PREDICTOR_NAMES.index(predictor)][1].signature.fields
        )
        # Routing assigns the LM at call time; a loaded Program carries none.
        assert predict.lm is None
    # And the module's own `dump_state` reproduces the envelope it was built from.
    assert program.dump_state() == with_demo.predictor_documents()
    assert with_demo.demos_for("taxonomy") == (demo,)
    assert with_demo.changed_predictors(stable) == ("taxonomy",)


def test_state_codec_round_trips_canonical_json() -> None:
    state = _candidate()
    document = encode_program_state(state)

    assert document.endswith("\n")
    assert decode_program_state(document) == state
    assert json.loads(document)["program_sha256"] == state.program_sha256


@pytest.mark.parametrize(
    ("updates", "code"),
    [
        ({"schema_version": "news_program_state_v2"}, "news_program_state_version_unsupported"),
        ({"dspy_version": "3.4.0"}, "news_program_state_dspy_version_unsupported"),
        ({"predictors": ["taxonomy", "event_semantics", "reader_card"]}, "news_program_state_schema_invalid"),
        ({"extra": "forbidden"}, "news_program_state_schema_invalid"),
    ],
)
def test_state_codec_fails_closed_on_version_predictor_order_or_extra_fields(
    updates: dict[str, Any], code: str
) -> None:
    with pytest.raises(ValueError, match=code):
        decode_program_state(json.dumps(_mutated(**updates)))


def test_loader_refuses_a_state_that_names_a_model_route() -> None:
    raw = load_stable_program_state().model_dump(mode="json")
    raw["state"]["taxonomy"]["lm"] = {"model": "openai/somewhere-else"}

    with pytest.raises(ValueError, match="news_program_state_schema_invalid"):
        decode_program_state(json.dumps(raw))
    with pytest.raises(ValueError, match="news_program_state_lm_route_forbidden"):
        NewsProgramStateV1.model_validate(raw)


def test_loader_refuses_an_unknown_or_missing_predictor() -> None:
    unknown = load_stable_program_state().model_dump(mode="json")
    unknown["state"]["progression_review"] = unknown["state"].pop("taxonomy")

    with pytest.raises(ValueError, match="news_program_state_predictor_set_invalid"):
        NewsProgramStateV1.model_validate(unknown)

    missing = load_stable_program_state().model_dump(mode="json")
    missing["state"].pop("reader_card")
    with pytest.raises(ValueError, match="news_program_state_predictor_set_invalid"):
        NewsProgramStateV1.model_validate(missing)


def test_loader_refuses_a_demo_whose_fields_are_not_the_signature_s() -> None:
    raw = load_stable_program_state().model_dump(mode="json")
    raw["state"]["taxonomy"]["demos"] = [{"evidence_json": "<evidence>", "semantics": {}}]

    with pytest.raises(ValueError, match="news_program_state_demo_fields_invalid"):
        NewsProgramStateV1.model_validate(raw)


def test_loader_refuses_a_truncated_signature_or_a_carried_scratch_list() -> None:
    truncated = load_stable_program_state().model_dump(mode="json")
    truncated["state"]["reader_card"]["signature"]["fields"] = truncated["state"]["reader_card"]["signature"]["fields"][
        :1
    ]
    with pytest.raises(ValueError, match="news_program_state_signature_fields_invalid"):
        NewsProgramStateV1.model_validate(truncated)

    scratch = load_stable_program_state().model_dump(mode="json")
    scratch["state"]["taxonomy"]["traces"] = [{"anything": 1}]
    with pytest.raises(ValueError, match="news_program_state_scratch_not_empty"):
        NewsProgramStateV1.model_validate(scratch)


def test_loader_refuses_an_out_of_bounds_instruction() -> None:
    raw = load_stable_program_state().model_dump(mode="json")
    raw["state"]["taxonomy"]["signature"]["instructions"] = "   "

    with pytest.raises(ValueError, match="news_program_instruction_empty"):
        NewsProgramStateV1.model_validate(raw)


def test_identity_ignores_the_lm_entry_but_nothing_else() -> None:
    stable = load_stable_program_state()
    payload = stable.model_dump(mode="json", exclude={"program_sha256"})
    without_lm = copy.deepcopy(payload)
    for document in without_lm["state"].values():
        document.pop("lm")

    from tracefold.news.artifact_identity import canonical_sha

    assert stable.program_sha256 == canonical_sha(
        {
            "schema_version": payload["schema_version"],
            "dspy_version": payload["dspy_version"],
            "predictors": payload["predictors"],
            "state": without_lm["state"],
        }
    )


def test_state_codec_rejects_nonfinite_json() -> None:
    raw = load_stable_program_state().model_dump(mode="json")
    raw["not_a_contract_field"] = float("nan")

    with pytest.raises(ValueError, match="nonfinite"):
        decode_program_state(json.dumps(raw))


def test_one_predictor_merge_keeps_the_other_two_byte_identical() -> None:
    stable = load_stable_program_state()
    merged = stable.with_predictor_document(
        "reader_card",
        predictor_document("reader_card", instruction=stable.instruction_for("reader_card") + "\nOne more line."),
    )

    assert merged.changed_predictors(stable) == ("reader_card",)
    assert merged.predictor_document("event_semantics") == stable.predictor_document("event_semantics")
    assert merged.predictor_document("taxonomy") == stable.predictor_document("taxonomy")
    assert merged.program_sha256 == merged.computed_sha256() != stable.program_sha256


def test_registry_refuses_an_unregistered_identity() -> None:
    with pytest.raises(ValueError, match="news_program_state_not_registered"):
        load_program_state("f" * 64)


def test_missing_candidate_path_has_a_stable_error(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="news_program_state_path_invalid"):
        read_program_state_document(str(tmp_path / "missing.json"))


def test_candidate_write_is_idempotent_but_refuses_same_identity_different_document(tmp_path: Path) -> None:
    state = _candidate()
    path = Path(write_program_candidate_state(state, artifact_root=tmp_path))

    assert Path(write_program_candidate_state(state, artifact_root=tmp_path)) == path
    path.write_text("{}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="news_program_compile_artifact_collision"):
        write_program_candidate_state(state, artifact_root=tmp_path)
