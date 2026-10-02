"""Manifest corruption and exact historical scopes cannot quietly change replay."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, replace
from pathlib import Path

import numpy as np
import pytest

from scripts.eval_news_recall import (
    EvaluationQuery,
    ReaderCase,
    VectorBundle,
    acceptance,
    build_chains,
    build_queries,
    build_reader_cases,
    chain_metrics,
    file_sha,
    fit,
    key_for,
    load_vectors,
    metrics,
    reader_selection,
    receipt_positive_labels,
)
from tests.support.news_update_semantic import STAMP, update_one
from tracefold.news.claim_recall import CALIBRATION, Candidate, Probe, vector_bytes
from tracefold.news.notifications.novelty import ReaderNovelty
from tracefold.news.updates.contracts import EventUpdate, content_revision_for
from tracefold.news.updates.identity import digest


def vector() -> bytes:
    return vector_bytes([1.0, *([0.0] * (CALIBRATION.embedder.dimensions - 1))], CALIBRATION.embedder)


def bundle_files(tmp_path: Path) -> tuple[Path, Path]:
    _, extraction, head = update_one()
    claim = head.claims[0]
    second = claim.model_copy(update={"statement": "A different exact text version."})
    claims = [claim, second]
    data = {
        "claims": [
            {
                "key": key_for(c),
                "ref": c.ref,
                "text_sha256": key_for(c).rsplit(":", 1)[1],
                "text": c.statement,
                "claim": c.model_dump(mode="json"),
                "event_id": head.event_id,
            }
            for c in claims
        ],
        "slots": [
            {
                "key": "analysis:a",
                "analysis_id": "analysis",
                "slot": "a",
                "event_id": head.event_id,
                "text": extraction.claims[0].statement,
                "fields": extraction.claims[0].fields.model_dump(mode="json"),
                "completed": STAMP + 4,
                "adopted": STAMP + 5,
            }
        ],
        "window_slots": {"original_audit": ["analysis:a"], "business": ["analysis:a"]},
    }
    source = tmp_path / "source.json"
    source.write_text("{}")
    data["source_manifest_sha256"] = file_sha(source)
    for name in ("claim", "slot"):
        data[f"{name}_texts_sha256"] = digest([r["text"] for r in data[f"{name}s"]])
    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps(data))
    matrix = np.zeros((2, CALIBRATION.embedder.dimensions), dtype="<f4")
    matrix[:, 0] = 1
    np.savez(tmp_path / "vectors.npz", claim_vectors=matrix, slot_vectors=matrix[:1])
    manifest = {
        "version": "claim_vector_manifest_v1",
        "embedder": asdict(CALIBRATION.embedder),
        "lexical_template": CALIBRATION.lexical_template,
        "input_file": "input.json",
        "array_file": "vectors.npz",
        "input_file_sha256": file_sha(input_path),
        "array_file_sha256": file_sha(tmp_path / "vectors.npz"),
        "source_manifest_sha256": file_sha(source),
        "wrapper": {
            "pooling_include_prompt": True,
            "query_instruction": "",
            "actual_parameter_dtype": CALIBRATION.embedder.dtype,
            "pooling_config": {
                "pooling_mode": CALIBRATION.embedder.pooling,
                "embedding_dimension": CALIBRATION.embedder.dimensions,
            },
        },
    }
    for name, array in (("claim", matrix), ("slot", matrix[:1])):
        keys, texts = [r["key"] for r in data[f"{name}s"]], [r["text"] for r in data[f"{name}s"]]
        manifest.update(
            {
                f"{name}_keys": keys,
                f"{name}_keys_sha256": digest(keys),
                f"{name}_texts_sha256": digest(texts),
                f"{name}s_shape": list(array.shape),
                f"{name}_array_sha256": hashlib.sha256(array.tobytes()).hexdigest(),
            }
        )
    target = tmp_path / "manifest.json"
    target.write_text(json.dumps(manifest))
    return target, source


@pytest.mark.parametrize(
    "mutation,reason",
    [
        ("order", "claim_order"),
        ("identity", "embedder_identity"),
        ("array", "array_file_digest"),
        ("text", "claim_texts_digest"),
        ("query", "query_slot_binding"),
    ],
)
def test_corrupt_or_misbound_vectors_fail_before_replay(tmp_path: Path, mutation: str, reason: str) -> None:
    target, source = bundle_files(tmp_path)
    assert len(load_vectors(target, source_manifest=source, calibration=CALIBRATION).claims) == 2
    manifest = json.loads(target.read_text())
    data = json.loads((tmp_path / "input.json").read_text())
    if mutation == "order":
        manifest["claim_keys"].reverse()
    elif mutation == "identity":
        manifest["embedder"]["revision"] = "0" * 40
    elif mutation == "array":
        with (tmp_path / "vectors.npz").open("ab") as stream:
            stream.write(b"changed")
    elif mutation == "text":
        manifest["claim_texts_sha256"] = "0" * 64
    else:
        data["slots"][0]["slot"] = "different-slot"
        (tmp_path / "input.json").write_text(json.dumps(data))
        manifest["input_file_sha256"] = file_sha(tmp_path / "input.json")
    target.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match=reason):
        load_vectors(target, source_manifest=source, calibration=CALIBRATION)


def frozen_scope() -> tuple[VectorBundle, dict, list, dict, list]:
    _, extraction, base = update_one()
    stamp = STAMP + 10_000
    old = base.claims[0].model_copy(
        update={
            "ref": "foreign-ref",
            "statement": "Old frozen wording.",
            "first_available_at_ms": stamp - 8 * 86400_000,
        }
    )
    current = old.model_copy(update={"statement": "Current adopted wording.", "first_available_at_ms": stamp - 1000})
    masked = current.model_copy(update={"ref": "masked-ref", "statement": "Retired wording."})
    future = current.model_copy(update={"statement": "Future wording."})
    query = base.claims[0].model_copy(update={"ref": "query-ref", "first_available_at_ms": stamp - 1})
    own = query.model_copy(update={"ref": "own-ref", "statement": "Own previous fact."})

    def snapshot(analysis, event, claims, adopted, **updates):
        head = base.model_copy(
            update={
                "event_id": event,
                "claims": tuple(claims),
                "adopted_at_ms": adopted,
                "content_revision": analysis,
                "previous_content_revision": None,
                "evidence_relations": (),
                "changes": (),
                **updates,
            }
        )
        content_sha = digest(head.content_material())
        head = head.model_copy(
            update={"content_sha": content_sha, "content_revision": content_revision_for(content_sha, None)}
        )
        return {
            "analysis_id": analysis,
            "event_id": event,
            "adopted": adopted,
            "completed": adopted - 1,
            "document": head.model_dump(mode="json"),
            "understanding": {"claims": [], "relations": []},
            "input_manifest": {},
        }

    rows = [
        snapshot("old", "foreign", [old], stamp - 8 * 86400_000 + 1),
        snapshot("current", "foreign", [current, masked], stamp - 999, retired_claim_refs=(masked.ref,)),
        snapshot("own", "query", [own], stamp - 500),
        snapshot("query", "query", [query], stamp + 1),
        snapshot("future", "foreign", [future], stamp + 200),
    ]
    rows[3]["understanding"] = {
        "claims": [extraction.claims[0].model_dump(mode="json")],
        "relations": [{"slot": "a", "previous_ref": ref} for ref in (old.ref, own.ref, "missing-ref")],
    }
    receipt = {
        "intent_id": "frozen-intent",
        "event_id": "foreign",
        "state": "sent",
        "settled": stamp - 10,
        "sent_claims": [old.model_dump(mode="json")],
        "body": "Exact delivered body.",
        "payload_sha256": digest("Exact delivered body."),
    }
    claims = {key_for(c): c for c in (old, current, masked, own, query, future)}
    slot = {
        "key": "query:a",
        "analysis_id": "query",
        "slot": "a",
        "event_id": "query",
        "text": extraction.claims[0].statement,
        "fields": extraction.claims[0].fields.model_dump(mode="json"),
        "completed": stamp,
        "adopted": stamp + 1,
    }
    bundle = VectorBundle(
        CALIBRATION.embedder,
        claims,
        {key: vector() for key in claims},
        {"query:a": slot},
        {"query:a": vector()},
        {"original_audit": ["query:a"]},
        {},
        "test-manifest",
    )
    sample = [
        {
            "analysis_id": "query",
            "slot": "a",
            "ref": query.ref,
            "label_query_id": "Q001",
            "completed": stamp,
            "adopted": stamp + 1,
            "p2": {"as_of": stamp + 2, "message_intents": []},
        }
    ]
    units = {
        "Q001": {
            "units": [
                {"kind": "C", "exact_version_keys": [key_for(old)], "label": "SF"},
                {"kind": "C", "exact_version_keys": [key_for(current)], "label": "U"},
                {"kind": "R", "intents": ["frozen-intent"], "label": "SF"},
            ]
        }
    }
    return bundle, {"all_snapshots": rows, "claims_raw": [], "receipts": [receipt]}, sample, units, sample


def test_current_masks_future_heads_and_sent_old_exact_version_are_rebuilt_at_query_time() -> None:
    bundle, facts, sample, units, original = frozen_scope()
    queries, _ = build_queries(bundle, facts, sample, units, original, window="original_audit")
    (q,) = queries
    keys_by_statement = {c.statement: key for key, c in bundle.claims.items()}
    assert q.prior_valid == {keys_by_statement["Old frozen wording."], keys_by_statement["Current adopted wording."]}
    assert keys_by_statement["Retired wording."] in {c.key for c in q.prior}
    assert keys_by_statement["Future wording."] not in {c.key for c in q.prior}
    assert all(not c.key.startswith("own-ref:") for c in q.prior)
    assert (q.own_baseline, q.foreign_baseline, q.unknown_baseline) == (1, 1, 1)
    old = keys_by_statement["Old frozen wording."]
    assert next(c for c in q.prior if c.key == old).sent
    assert q.prior_labels[old] == "SF" and q.prior_labels[keys_by_statement["Current adopted wording."]] == "U"
    assert q.receipt_valid == {"frozen-intent:" + old}
    assert q.prior_probe.text == bundle.slots[q.key]["text"]


def test_unknown_outside_the_pool_stays_unknown_and_foreign_reduction_has_its_own_denominator() -> None:
    candidates = (Candidate("gold", lexical=0.8), Candidate("known-u", lexical=0.7), Candidate("outside", lexical=0.6))
    q = EvaluationQuery(
        "analysis:a",
        "Q001",
        "zh",
        Probe("policy"),
        "policy",
        None,
        None,
        candidates,
        (),
        frozenset(c.key for c in candidates),
        frozenset(),
        {"gold": "SF", "known-u": "U"},
        {},
        own_baseline=2,
        foreign_baseline=10,
    )
    score = metrics([q], replace(CALIBRATION, prior=replace(CALIBRATION.prior, k=3)), "prior")
    assert (score["unknown_selected"], score["unrelated_selected"], score["labeled_selected"]) == (1, 1, 2)
    assert score["foreign_comparison_ratio"] == 0.3
    assert score["total_baseline"] == 12 and score["total_comparisons"] == 5
    assert score["total_comparison_ratio"] == 5 / 12
    assert acceptance(score, score, {"max_total_comparison_ratio": 0.4}) == ["max_total_comparison_ratio"]
    q.prior_valid = frozenset({"known-u", "outside"})
    missing = metrics([q], CALIBRATION, "prior")
    assert missing["sf_queries"] == 1 and missing["sf_success"] == 0
    assert missing["sf_gold_outside_scope_queries"] == 1


def test_fit_recomputes_both_policies_and_cannot_accept_unknown_coverage_as_success() -> None:
    rows = (Candidate("gold", lexical=0.9),)
    q = EvaluationQuery(
        "analysis:a",
        "Q001",
        "zh",
        Probe("policy"),
        "policy",
        Probe("policy"),
        "policy",
        rows,
        rows,
        frozenset({"gold"}),
        frozenset({"gold"}),
        {"gold": "SF"},
        {"gold": "SF"},
    )
    q.reader_case = ReaderCase("query", "ref", q, ReaderNovelty(novelty="unlinked"), frozenset({"gold"}))
    grid = {
        "version": "claim_recall_fit_grid_v1",
        "prior": [asdict(CALIBRATION.prior)],
        "receipt": [asdict(CALIBRATION.receipt)],
        "acceptance": {
            route: {
                "min_sf_success_rate": 1.0,
                "max_unknown_selected_fraction": 0.0,
                "min_degraded_sf_success_rate": 1.0,
            }
            for route in ("prior", "receipt")
        },
    }
    fitted, report = fit([q], CALIBRATION, grid)
    assert fitted is not None and all(not rows[0]["failures"] for rows in report.values())
    q.receipt_labels = {}
    fitted, report = fit([q], CALIBRATION, grid)
    assert fitted is None and report["receipt"][0]["failures"]


def test_final_reader_selection_keeps_linked_prefix_beyond_rank_k_and_unknown_chains_do_not_hit() -> None:
    rows = (Candidate("ordinary", lexical=0.9),)
    q = EvaluationQuery(
        "decision:ref",
        None,
        "other",
        Probe(""),
        "",
        Probe("text"),
        "text",
        (),
        rows,
        frozenset(),
        frozenset({"ordinary"}),
        {},
        {},
    )
    case = ReaderCase(
        "push",
        "ref",
        q,
        ReaderNovelty(novelty="known", linked_intents=("older-gold",)),
        frozenset({"older-gold", "ordinary"}),
    )
    policy = replace(CALIBRATION, receipt=replace(CALIBRATION.receipt, k=1))
    assert reader_selection(case, policy).intent_ids == ("older-gold", "ordinary")
    result = chain_metrics(
        [case], {"push": {"older-gold"}, "lost-input": {"missing"}}, {"lost-input": "missing"}, policy
    )
    assert result["hits"] == 1 and result["sf_pushes"] == 2 and result["recall"] == 0.5
    assert result["misses"] == [] and result["unknown"] == {"lost-input": "missing"}

    linked = tuple(f"linked-{n}" for n in range(16))
    q.receipt_labels = {"ordinary": "SF"}
    q.reader_case = replace(
        case, novelty=ReaderNovelty(novelty="known", linked_intents=linked), available=frozenset((*linked, "ordinary"))
    )
    score = metrics([q], policy, "receipt")
    assert score["raw_ranked_sf_success"] == 1
    assert score["sf_success"] == 0 and len(score["selections"][q.key]["keys"]) == 16


def test_duplicate_replay_uses_exact_head_first_input_and_only_already_sent_versions() -> None:
    bundle, facts, sample, units, original = frozen_scope()
    _, index = build_queries(bundle, facts, sample, units, original, window="original_audit")
    row = facts["all_snapshots"][3]
    head = EventUpdate.model_validate(row["document"])
    decision = {
        "intent_id": "query-intent",
        "state": "sent",
        "notification_id": "decision",
        "decided": row["adopted"] + 4,
        "started": row["adopted"] + 2,
        "event_id": head.event_id,
        "update_ref": head.ref,
        "claim_decisions": [{"claim_ref": "query-ref", "decision": "notify"}],
    }
    facts["original_audit_decisions"] = [decision]
    facts["receipts"].append(
        {
            **facts["receipts"][0],
            "intent_id": "query-intent",
            "event_id": head.event_id,
            "settled": row["adopted"] + 5,
            "sent_claims": [head.claims[0].model_dump(mode="json")],
        }
    )
    cases, expected, unknown = build_chains(
        bundle,
        facts,
        {"SF_pairs": [{"p": "query-intent", "c": "frozen-intent"}]},
        index,
    )
    assert unknown == {} and expected == {"query-intent": {"frozen-intent"}}
    assert len(cases) == 1 and cases[0].available == {"frozen-intent"}
    assert all(c.group == "frozen-intent" for c in cases[0].query.receipt)
    decision["started"] = row["adopted"]
    cases, unknown = build_reader_cases(bundle, facts, {"query-intent": decision}, index)
    assert cases == [] and unknown == {"query-intent": "decision_exact_head_time_mismatch"}


def test_original_receipt_gold_transfers_only_reviewed_exact_positive_versions() -> None:
    labels = receipt_positive_labels(
        {"ref:old-version": "SF", "ref:new-version": "U", "second:version": "SD"},
        {"known-card": "U", "already-sf": "SF"},
        {
            "known-card": ["ref:old-version"],
            "unreviewed-but-proved": ["ref:old-version"],
            "wrong-version": ["ref:new-version"],
            "negative-only": ["ref:new-version", "missing:version"],
            "increment": ["second:version"],
            "already-sf": ["second:version"],
        },
    )
    assert labels == {
        "known-card": "SF",
        "already-sf": "SF",
        "unreviewed-but-proved": "SF",
        "increment": "SD",
    }


def test_missing_reader_inputs_retain_reviewed_sf_denominator_and_invalid_thresholds_fail() -> None:
    q = EvaluationQuery(
        "analysis:a",
        "Q001",
        "ru",
        Probe("text"),
        "text",
        None,
        None,
        (),
        (),
        frozenset(),
        frozenset(),
        {},
        {"receipt": "SF"},
    )
    score = metrics([q], CALIBRATION, "receipt")
    assert score["missing_sf_input_queries"] == score["sf_queries"] == 1
    assert score["sf_success_rate"] == 0
    assert score["strata"]["ru"] == {"total": 1, "success": 0}
    grid = {
        "version": "claim_recall_fit_grid_v1",
        "prior": [asdict(CALIBRATION.prior)],
        "receipt": [asdict(CALIBRATION.receipt)],
        "acceptance": {consumer: {"max_unknown_selected_fraction": float("nan")} for consumer in ("prior", "receipt")},
    }
    with pytest.raises(ValueError, match="fit_threshold_invalid"):
        fit([q], CALIBRATION, grid)
