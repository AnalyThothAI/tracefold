"""Replay and fit the production ranker against immutable, exact-version facts.

Only PostgreSQL TEMP tables are written. No model, judgment-cache, or delivery
operation runs. Unknown labels remain separate from the complete reviewed pool.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Literal

import numpy as np
import psycopg
from psycopg.rows import dict_row

from tracefold.news.claim_recall import (
    CALIBRATION,
    LEXICAL_TEMPLATE,
    PRIOR_WINDOW_MS,
    RECEIPT_WINDOW_MS,
    TEXT_TEMPLATE,
    Calibration,
    Candidate,
    Cuts,
    EmbedderIdentity,
    Probe,
    embed_text,
    lexical_text,
    prepare_rank,
    rank,
    text_sha,
    vector_bytes,
)
from tracefold.news.notifications.novelty import ClaimLink, LinkedReceipt, ReaderNovelty, reader_novelty
from tracefold.news.notifications.recall import ClaimSelection, select_for_claim
from tracefold.news.storage.claim_index import ClaimIndexStorage, source_keys
from tracefold.news.storage.notification_context import delivered_text, listing_compatible_links
from tracefold.news.updates.contracts import Claim, DraftClaim, EventUpdate, Source
from tracefold.news.updates.identity import digest

Consumer = Literal["prior", "receipt"]
LABELS = frozenset({"SF", "SD", "TO", "U"})


def require(condition: bool, reason: str) -> None:
    if not condition:
        raise ValueError(reason)


def file_sha(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            value.update(block)
    return value.hexdigest()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def read_rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def relative_file(root: Path, name: str) -> Path:
    path = (root / name).resolve()
    require(path.is_relative_to(root.resolve()) and path.is_file(), "vector_manifest_file_invalid")
    return path


def load_calibration(path: Path | None) -> Calibration:
    if path is None:
        return CALIBRATION
    data = read_json(path)
    require(data["version"] == "claim_recall_v1", "calibration_version_invalid")
    require(data["lexical_template"] == LEXICAL_TEMPLATE, "calibration_lexical_identity_mismatch")
    return Calibration(
        **{
            **data,
            "embedder": EmbedderIdentity(**data["embedder"]),
            "prior": Cuts(**data["prior"]),
            "receipt": Cuts(**data["receipt"]),
        }
    )


@dataclass(frozen=True)
class VectorBundle:
    identity: EmbedderIdentity
    claims: Mapping[str, Claim]
    vectors: Mapping[str, bytes]
    slots: Mapping[str, Mapping[str, Any]]
    slot_vectors: Mapping[str, bytes]
    windows: Mapping[str, Sequence[str]]
    manifest: Mapping[str, Any]
    manifest_sha: str


def load_vectors(manifest_path: Path, *, source_manifest: Path, calibration: Calibration) -> VectorBundle:
    manifest = read_json(manifest_path)
    require(manifest["version"] == "claim_vector_manifest_v1", "vector_manifest_version_invalid")
    identity = EmbedderIdentity(**manifest["embedder"])
    require(identity == calibration.embedder, "vector_manifest_model_identity_mismatch")
    require(
        bool(re.fullmatch(r"[a-f0-9]{40}", identity.revision))
        and identity.template == TEXT_TEMPLATE
        and identity.normalization == "l2"
        and identity.dimensions > 0
        and 1 <= identity.max_tokens <= 8192,
        "vector_manifest_identity_invalid",
    )
    require(manifest["lexical_template"] == calibration.lexical_template == LEXICAL_TEMPLATE, "vector_lexical_mismatch")
    wrapper = manifest["wrapper"]
    require(
        wrapper["pooling_include_prompt"] is True
        and wrapper["query_instruction"] == ""
        and wrapper["actual_parameter_dtype"] == identity.dtype
        and wrapper["pooling_config"]["pooling_mode"] == identity.pooling
        and wrapper["pooling_config"]["embedding_dimension"] == identity.dimensions,
        "vector_wrapper_mismatch",
    )
    source_sha = file_sha(source_manifest)
    require(manifest["source_manifest_sha256"] == source_sha, "vector_source_manifest_digest_mismatch")
    input_path = relative_file(manifest_path.parent, manifest["input_file"])
    array_path = relative_file(manifest_path.parent, manifest["array_file"])
    require(file_sha(input_path) == manifest["input_file_sha256"], "vector_input_file_digest_mismatch")
    require(file_sha(array_path) == manifest["array_file_sha256"], "vector_array_file_digest_mismatch")
    data = read_json(input_path)
    require(data["source_manifest_sha256"] == source_sha, "vector_input_source_digest_mismatch")
    claims: dict[str, Claim] = {}
    slots: dict[str, Mapping[str, Any]] = {}
    for row in data["claims"]:
        claim = Claim.model_validate(row["claim"])
        key = key_for(claim)
        require(
            row["key"] == key
            and row["ref"] == claim.ref
            and row["text_sha256"] == text_sha(claim)
            and row["text"] == embed_text(claim)
            and key not in claims,
            "vector_exact_claim_binding_invalid",
        )
        claims[key] = claim
    for row in data["slots"]:
        key = f"{row['analysis_id']}:{row['slot']}"
        require(row["key"] == key and key not in slots, "vector_query_slot_binding_invalid")
        slots[key] = row
    vectors: dict[str, bytes] = {}
    slot_vectors: dict[str, bytes] = {}
    with np.load(array_path, allow_pickle=False) as arrays:
        require(set(arrays.files) == {"claim_vectors", "slot_vectors"}, "vector_array_names_invalid")
        for name, rows, destination in (("claim", data["claims"], vectors), ("slot", data["slots"], slot_vectors)):
            keys, texts = [r["key"] for r in rows], [r["text"] for r in rows]
            matrix = arrays[f"{name}_vectors"]
            require(manifest[f"{name}_keys"] == keys, f"vector_{name}_order_mismatch")
            require(digest(keys) == manifest[f"{name}_keys_sha256"], f"vector_{name}_keys_digest_mismatch")
            require(digest(texts) == manifest[f"{name}_texts_sha256"], f"vector_{name}_texts_digest_mismatch")
            require(data[f"{name}_texts_sha256"] == digest(texts), f"vector_input_{name}_texts_digest_mismatch")
            require(
                matrix.dtype == np.dtype("<f4")
                and matrix.shape == (len(rows), identity.dimensions)
                and list(matrix.shape) == manifest[f"{name}s_shape"]
                and np.isfinite(matrix).all(),
                f"vector_{name}_shape_or_dtype_invalid",
            )
            require(
                hashlib.sha256(matrix.tobytes(order="C")).hexdigest() == manifest[f"{name}_array_sha256"],
                f"vector_{name}_array_digest_mismatch",
            )
            require(bool(np.all(np.abs(np.linalg.norm(matrix, axis=1) - 1.0) <= 0.001)), "vector_not_l2_normalized")
            destination.update((key, vector_bytes(v, identity)) for key, v in zip(keys, matrix, strict=True))
    for keys in data["window_slots"].values():
        require(len(keys) == len(set(keys)) and set(keys) <= slots.keys(), "vector_window_binding_invalid")
    return VectorBundle(
        identity, claims, vectors, slots, slot_vectors, data["window_slots"], manifest, file_sha(manifest_path)
    )


def load_source_files(manifest_path: Path) -> dict[str, list[dict[str, Any]]]:
    selected = {}
    for row in read_json(manifest_path)["files"]:
        if row["name"] not in {"all_snapshots", "claims_raw", "receipts", "original_audit_decisions"}:
            continue
        path = Path(row["path"])
        require(file_sha(path) == row["sha256"], f"source_file_digest_mismatch:{row['name']}")
        values = read_rows(path)
        require(len(values) == row["rows"], f"source_row_count_mismatch:{row['name']}")
        require(row["name"] not in selected, "source_manifest_duplicate")
        selected[row["name"]] = values
    require(
        set(selected) == {"all_snapshots", "claims_raw", "receipts", "original_audit_decisions"},
        "source_manifest_facts_missing",
    )
    return selected


def load_labels(root: Path, provenance_path: Path) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    sample = read_json(root / "data/sample_bound.json")
    units = read_json(root / "data/units_bound.json")
    provenance = read_json(provenance_path)
    require(
        provenance["all_reads_start1"] is True
        and provenance["reads_with_truncation_marker"] == 0
        and provenance["reads"] > 0
        and provenance["units"] == sum(len(q["units"]) for q in units.values())
        and provenance["queries"] == len(sample) == len(units),
        "label_complete_pool_provenance_missing",
    )
    ids = [q["label_query_id"] for q in sample]
    require(len(ids) == len(set(ids)) and set(ids) == set(units), "label_query_binding_invalid")
    for q in sample:
        require(units[q["label_query_id"]]["ref"] == q["ref"], "label_query_ref_binding_invalid")
    for value in units.values():
        seen = set()
        for unit in value["units"]:
            require(unit["id"] not in seen and unit["label"] in LABELS, "label_unit_invalid")
            seen.add(unit["id"])
            require(unit["kind"] in {"C", "R"}, "label_unit_kind_invalid")
            require(
                bool(unit["exact_version_keys" if unit["kind"] == "C" else "intents"]), "label_exact_binding_missing"
            )
    return sample, units, provenance


@dataclass(frozen=True)
class Snapshot:
    row: Mapping[str, Any]
    head: EventUpdate
    drafts: Mapping[str, DraftClaim]


@dataclass
class EvaluationQuery:
    key: str
    label_id: str | None
    script: str
    prior_probe: Probe
    prior_lexical: str
    receipt_probe: Probe | None
    receipt_lexical: str | None
    prior: tuple[Candidate, ...]
    receipt: tuple[Candidate, ...]
    prior_valid: frozenset[str]
    receipt_valid: frozenset[str]
    prior_labels: dict[str, str]
    receipt_labels: dict[str, str]
    own_baseline: int = 0
    foreign_baseline: int = 0
    unknown_baseline: int = 0
    receipt_baseline: int | None = None
    source_scope_known: bool = True
    baseline_equivalent_links: int = 0
    baseline_sf_known: bool = False
    baseline_sf_unknown: bool = False
    reader_case: ReaderCase | None = None
    reader_input_error: str | None = None
    receipt_baseline_intents: tuple[str, ...] = ()


def key_for(claim: Claim) -> str:
    return f"{claim.ref}:{text_sha(claim)}"


def heads_at(snapshots: Sequence[Snapshot], stamp: int) -> dict[str, Snapshot]:
    heads: dict[str, Snapshot] = {}
    for item in snapshots:
        if item.row["adopted"] >= stamp:
            break
        previous = heads.get(item.head.event_id)
        if previous is not None and previous.row["adopted"] == item.row["adopted"]:
            if previous.head.previous_content_revision == item.head.content_revision:
                continue
            require(
                item.head.previous_content_revision == previous.head.content_revision,
                "snapshot_adoption_order_ambiguous",
            )
        heads[item.head.event_id] = item
    return heads


def label_map(units: Sequence[dict[str, Any]], kind: str) -> dict[str, str]:
    labels: dict[str, str] = {}
    for unit in units:
        if unit["kind"] != kind:
            continue
        for key in unit["exact_version_keys" if kind == "C" else "intents"]:
            require(key not in labels or labels[key] == unit["label"], "label_conflicting_exact_version")
            labels[key] = unit["label"]
    return labels


def receipt_positive_labels(
    claim_labels: Mapping[str, str],
    receipt_labels: Mapping[str, str],
    frozen_versions: Mapping[str, Sequence[str]],
) -> dict[str, str]:
    """Original audit contract: a reviewed exact SF/SD claim proves card relevance.

    A negative claim cannot judge the rest of a full card. Pool-external cards
    stay unknown unless their frozen exact version provides positive proof.
    """
    labels = dict(receipt_labels)
    priority = {"U": 0, "TO": 1, "SD": 2, "SF": 3}
    for intent, keys in frozen_versions.items():
        positives = [claim_labels[key] for key in keys if claim_labels.get(key) in {"SF", "SD"}]
        if not positives:
            continue
        strongest = max(positives, key=priority.__getitem__)
        if intent not in labels or priority[strongest] > priority[labels[intent]]:
            labels[intent] = strongest
    return labels


def script_of(text: str) -> str:
    return "zh" if re.search(r"[\u4e00-\u9fff]", text) else "ru" if re.search(r"[\u0400-\u04ff]", text) else "other"


def claim_sources(claim: Claim | DraftClaim, head: EventUpdate) -> frozenset[str]:
    evidence = {e.ref: e.source for e in head.evidence}
    return frozenset(
        key for c in claim.citations if c.evidence_ref in evidence for key in source_keys(evidence[c.evidence_ref])
    )


def build_queries(
    bundle: VectorBundle,
    facts: Mapping[str, list[dict[str, Any]]],
    sample: list[dict[str, Any]],
    units: Mapping[str, Any],
    original_queries: list[dict[str, Any]],
    *,
    window: str,
    query_keys: Sequence[str] | None = None,
) -> tuple[list[EvaluationQuery], Mapping[str, tuple[Claim, frozenset[str]]]]:
    snapshots = []
    for row in facts["all_snapshots"]:
        if row["document"] is None:
            continue
        head = EventUpdate.model_validate(row["document"])
        require(head.event_id == row["event_id"] and head.adopted_at_ms == row["adopted"], "snapshot_identity_mismatch")
        drafts = {
            c.slot: c
            for value in (row["understanding"] or {}).get("claims", [])
            for c in (DraftClaim.model_validate(value),)
        }
        snapshots.append(Snapshot(row, head, drafts))
    snapshots.sort(key=lambda s: (s.row["adopted"], s.row["analysis_id"]))
    by_analysis = {s.row["analysis_id"]: s for s in snapshots}
    require(len(by_analysis) == len(snapshots), "snapshot_duplicate_analysis")
    current_by_analysis = {
        s.row["analysis_id"]: frozenset(key_for(c) for c in s.head.current_claims) for s in snapshots
    }
    index: dict[str, tuple[Claim, frozenset[str]]] = {}
    first_adopted: dict[str, int] = {}
    ownership: dict[str, str] = {}
    for snapshot in snapshots:
        for claim in snapshot.head.claims:
            key = key_for(claim)
            require(
                claim.ref not in ownership or ownership[claim.ref] == snapshot.head.event_id, "claim_owner_ambiguous"
            )
            ownership[claim.ref] = snapshot.head.event_id
            index.setdefault(key, (claim, claim_sources(claim, snapshot.head)))
            first_adopted.setdefault(key, int(snapshot.row["adopted"]))
    receipts = sorted(facts["receipts"], key=lambda r: (r["settled"], r["intent_id"]))
    require(len({r["intent_id"] for r in receipts}) == len(receipts), "receipt_intent_duplicate")
    frozen_by_intent = {
        r["intent_id"]: tuple(Claim.model_validate(c) for c in r["sent_claims"] or ()) for r in receipts
    }
    frozen_keys = {intent: tuple(key_for(c) for c in claims) for intent, claims in frozen_by_intent.items()}
    valid_bodies = {
        r["intent_id"]
        for r in receipts
        if r["state"] == "sent" and delivered_text({**r, "settled_at_ms": r["settled"]}) is not None
    }
    for receipt in receipts:
        for claim, key in zip(frozen_by_intent[receipt["intent_id"]], frozen_keys[receipt["intent_id"]], strict=True):
            require(
                claim.ref not in ownership or ownership[claim.ref] == receipt["event_id"],
                "receipt_claim_owner_mismatch",
            )
            ownership[claim.ref] = receipt["event_id"]
            index.setdefault(key, (claim, frozenset()))
            if receipt["state"] == "sent":
                first_adopted.setdefault(key, int(receipt["settled"]))
    require(set(index) == set(bundle.claims), "vector_frozen_version_pool_mismatch")
    require(
        all(
            key_for(Claim.model_validate(r["claim"])) in index and ownership[r["claim"]["ref"]] == r["event_id"]
            for r in facts["claims_raw"]
        ),
        "source_raw_claim_projection_mismatch",
    )
    sampled = {f"{q['analysis_id']}:{q['slot']}": q for q in sample}
    original = {f"{q['analysis_id']}:{q['slot']}": q for q in original_queries}
    require(len(sampled) == len(sample) and len(original) == len(original_queries), "audit_query_slot_duplicate")
    require(set(sampled) <= set(bundle.windows["original_audit"]), "label_sample_slot_outside_window")
    queries = []
    embedder_key = bundle.identity.key
    selected_keys = bundle.windows[window] if query_keys is None else query_keys
    require(
        len(selected_keys) == len(set(selected_keys)) and set(selected_keys) <= set(bundle.windows[window]),
        "replay_query_subset_invalid",
    )
    for query_key in selected_keys:
        slot = bundle.slots[query_key]
        snapshot = by_analysis[slot["analysis_id"]]
        draft = snapshot.drafts[slot["slot"]]
        require(
            draft.statement == slot["text"]
            and draft.fields.model_dump(mode="json") == slot["fields"]
            and snapshot.head.event_id == slot["event_id"]
            and slot["completed"] == snapshot.row["completed"]
            and slot["adopted"] == snapshot.row["adopted"],
            "query_exact_frozen_slot_mismatch",
        )
        stamp = int(slot["completed"])
        current = {
            key
            for event, s in heads_at(snapshots, stamp).items()
            if event != snapshot.head.event_id
            for key in current_by_analysis[s.row["analysis_id"]]
        }
        sent = {
            key
            for r in receipts
            if stamp - RECEIPT_WINDOW_MS <= r["settled"] < stamp
            and r["state"] == "sent"
            and r["event_id"] != snapshot.head.event_id
            for claim, key in zip(frozen_by_intent[r["intent_id"]], frozen_keys[r["intent_id"]], strict=True)
            if claim.first_available_at_ms < stamp
        }
        raw = {
            key
            for key, (claim, _) in index.items()
            if ownership[claim.ref] != snapshot.head.event_id
            and first_adopted.get(key, stamp) < stamp
            and stamp - PRIOR_WINDOW_MS <= claim.first_available_at_ms < stamp
        } | sent
        manifest_evidence = (snapshot.row["input_manifest"] or {}).get("evidence", [])
        sources = frozenset(key for e in manifest_evidence for key in source_keys(Source.model_validate(e["source"])))
        prior = tuple(
            Candidate(
                key,
                bundle.vectors[key],
                embedder_key,
                same_source=bool(index[key][1] & sources),
                sent=key in sent,
                group=index[key][0].ref,
            )
            for key in sorted(raw)
        )
        query_row = sampled.get(query_key, original.get(query_key))
        label_id = None if query_key not in sampled else sampled[query_key]["label_query_id"]
        unit_rows = () if label_id is None else units[label_id]["units"]
        prior_labels = label_map(unit_rows, "C")
        receipt_labels = label_map(unit_rows, "R")
        require(set(prior_labels) <= bundle.claims.keys(), "label_bound_version_missing")
        receipt_probe, receipt_lexical = None, None
        receipt_candidates: list[Candidate] = []
        receipt_valid: set[str] = set()
        receipt_baseline = None
        receipt_baseline_intents = ()
        if query_row is not None:
            require(
                query_row["adopted"] == slot["adopted"] and query_row["completed"] == slot["completed"],
                "audit_query_time_binding_mismatch",
            )
            claim = next((c for c in snapshot.head.current_claims if c.ref == query_row["ref"]), None)
            decision = query_row.get("p2")
            if claim is not None and decision is not None:
                receipt_stamp = int(decision["as_of"])
                receipt_sources = claim_sources(claim, snapshot.head)
                receipt_probe = Probe(claim.statement, bundle.vectors[key_for(claim)], embedder_key)
                receipt_lexical = lexical_text(claim)
                receipt_baseline = len(decision.get("message_intents") or ())
                receipt_baseline_intents = tuple(decision.get("message_intents") or ())
                for row in receipts:
                    if (
                        not receipt_stamp - RECEIPT_WINDOW_MS <= row["settled"] < receipt_stamp
                        or row["state"] != "sent"
                    ):
                        continue
                    valid_body = row["intent_id"] in valid_bodies
                    for _frozen, version in zip(
                        frozen_by_intent[row["intent_id"]], frozen_keys[row["intent_id"]], strict=True
                    ):
                        key = f"{row['intent_id']}:{version}"
                        receipt_candidates.append(
                            Candidate(
                                key,
                                bundle.vectors[version],
                                embedder_key,
                                same_source=bool(index[version][1] & receipt_sources),
                                group=row["intent_id"],
                            )
                        )
                        if valid_body:
                            receipt_valid.add(key)
                receipt_labels = receipt_positive_labels(
                    prior_labels,
                    receipt_labels,
                    {
                        r["intent_id"]: frozen_keys[r["intent_id"]]
                        for r in receipts
                        if r["state"] == "sent" and receipt_stamp - RECEIPT_WINDOW_MS <= r["settled"] < receipt_stamp
                    },
                )
        baseline: Counter[str] = Counter()
        for relation in (snapshot.row["understanding"] or {}).get("relations", []):
            if relation["slot"] != draft.slot:
                continue
            owner = ownership.get(relation["previous_ref"])
            baseline["unknown" if owner is None else "own" if owner == snapshot.head.event_id else "foreign"] += 1
            if owner is not None and owner != snapshot.head.event_id:
                baseline["equivalent"] += relation.get("relation") == "equivalent"
                if label_id is not None:
                    versions = [
                        key
                        for key, (c, _) in index.items()
                        if c.ref == relation["previous_ref"] and first_adopted.get(key, stamp) < stamp
                    ]
                    labels = {prior_labels.get(key) for key in versions}
                    if labels == {"SF"}:
                        baseline["sf_known"] += 1
                    elif not labels or None in labels or "SF" in labels:
                        baseline["sf_unknown"] += 1
        queries.append(
            EvaluationQuery(
                query_key,
                label_id,
                script_of(draft.statement),
                Probe(draft.statement, bundle.slot_vectors[query_key], embedder_key),
                lexical_text(draft),
                receipt_probe,
                receipt_lexical,
                prior,
                tuple(receipt_candidates),
                frozenset(raw & (current | sent)),
                frozenset(receipt_valid),
                prior_labels,
                receipt_labels,
                baseline["own"],
                baseline["foreign"],
                baseline["unknown"],
                receipt_baseline,
                bool(manifest_evidence),
                baseline["equivalent"],
                bool(baseline["sf_known"]),
                bool(baseline["sf_unknown"]),
            )
        )
        queries[-1].receipt_baseline_intents = receipt_baseline_intents
        if len(queries) % 500 == 0:
            print(json.dumps({"stage": "frozen_scopes", "queries": len(queries)}), flush=True)
    return queries, index


@dataclass(frozen=True)
class ReaderCase:
    push: str
    claim_ref: str
    query: EvaluationQuery
    novelty: ReaderNovelty
    available: frozenset[str]
    claim: Claim | None = None
    as_of_ms: int | None = None
    analysis_id: str | None = None
    links: tuple[ClaimLink, ...] = ()
    linked_receipts: tuple[LinkedReceipt, ...] = ()


def load_duplicate_labels(root: Path) -> dict[str, Any]:
    manifest = read_json(root / "duplicate_binding_manifest.json")
    research = Path(manifest["research_root"])
    require((research / "source/original-audit").resolve() == root.resolve(), "duplicate_audit_root_mismatch")
    for row in manifest["binding_files"]:
        require(file_sha(relative_file(research, row["file"])) == row["sha256"], "duplicate_binding_digest_mismatch")
    provenance_path = relative_file(research, manifest["read_provenance_file"])
    require(file_sha(provenance_path) == manifest["read_provenance_sha256"], "duplicate_provenance_digest_mismatch")
    data = read_json(root / "data/duplicates_bound.json")
    provenance = read_json(provenance_path)
    require(bool(provenance) and all(not p["error"] for p in provenance), "duplicate_read_provenance_invalid")
    for row in provenance:
        require(
            file_sha(relative_file(research, row["restored_file"])) == row["file_sha256"],
            "duplicate_label_file_digest_mismatch",
        )
    require(not data["errors"], "duplicate_exact_binding_failed")
    pairs = data["pairs"]
    require(len({p["cid"] for p in pairs}) == len(pairs), "duplicate_pair_id_ambiguous")
    sf = [p for p in pairs if p["label"] == "SF"]
    require(data["SF_pairs"] == sf and set(data["SF_pushes"]) == {p["p"] for p in sf}, "duplicate_sf_pool_mismatch")
    return data


def build_chains(
    bundle: VectorBundle,
    facts: Mapping[str, list[dict[str, Any]]],
    duplicates: Mapping[str, Any],
    index: Mapping[str, tuple[Claim, frozenset[str]]],
) -> tuple[list[ReaderCase], dict[str, set[str]], dict[str, str]]:
    """Replay each labeled push at its original latest sent decision, before the send.

    ClaimLink resolution, listing compatibility, two-hop novelty and final m1..m16
    selection remain production functions. Lost historical unsettled transitions
    are excluded explicitly; they cannot prove a delivered duplicate chain.
    """
    expected: dict[str, set[str]] = defaultdict(set)
    for pair in duplicates["SF_pairs"]:
        expected[pair["p"]].add(pair["c"])
    decisions = {}
    for decision in sorted(facts["original_audit_decisions"], key=lambda d: (d["decided"], d["notification_id"])):
        if decision["state"] == "sent" and decision["intent_id"] in expected:
            previous = decisions.get(decision["intent_id"])
            require(
                previous is None or previous["decided"] != decision["decided"], "duplicate_decision_order_ambiguous"
            )
            decisions[decision["intent_id"]] = decision
    frozen_intents = {r["intent_id"] for r in facts["receipts"]}
    require(
        set(expected) <= frozen_intents and set().union(*expected.values()) <= frozen_intents,
        "duplicate_receipt_binding_missing",
    )
    cases, unknown = build_reader_cases(bundle, facts, {push: decisions.get(push) for push in expected}, index)
    return cases, dict(expected), unknown


def build_reader_cases(
    bundle: VectorBundle,
    facts: Mapping[str, list[dict[str, Any]]],
    decisions: Mapping[str, Mapping[str, Any] | None],
    index: Mapping[str, tuple[Claim, frozenset[str]]],
    *,
    requested_refs: Mapping[str, frozenset[str]] | None = None,
) -> tuple[list[ReaderCase], dict[str, str]]:
    snapshots = []
    links = []
    seen = set()
    for row in sorted(facts["all_snapshots"], key=lambda r: (r["adopted"], r["analysis_id"])):
        if row["document"] is None:
            continue
        head = EventUpdate.model_validate(row["document"])
        snapshots.append(Snapshot(row, head, {}))
        for change in head.changes:
            key = (head.ref, change.current_ref, change.previous_ref)
            if key in seen or change.previous_ref == change.current_ref:
                continue
            seen.add(key)
            if change.relation in {"equivalent", "adds_information", "real_world_change", "corrects", "conflicts"}:
                links.append(
                    ClaimLink(
                        current_ref=change.current_ref,
                        previous_ref=change.previous_ref,
                        relation=change.relation,
                        asserted_at_ms=row["adopted"],
                    )
                )
    frozen = {r["intent_id"]: tuple(Claim.model_validate(c) for c in r["sent_claims"] or ()) for r in facts["receipts"]}
    cases, unknown = [], {}
    embedder_key = bundle.identity.key
    for push in sorted(decisions):
        decision = decisions.get(push)
        if decision is None or decision.get("started") is None:
            unknown[push] = "decision_or_exact_input_missing"
            continue
        stamp = int(decision["started"])
        snapshot = heads_at(snapshots, stamp).get(decision["event_id"])
        if snapshot is None or snapshot.head.ref != decision.get("update_ref"):
            unknown[push] = "decision_exact_head_time_mismatch"
            continue
        head = snapshot.head
        history = [link for link in links if link.asserted_at_ms < stamp]
        invalidated = {link.previous_ref for link in history if link.relation in {"corrects", "real_world_change"}}
        active = {c.ref: c for c in head.current_claims if c.ref not in invalidated}
        receipts = [
            r for r in facts["receipts"] if r["state"] == "sent" and stamp - RECEIPT_WINDOW_MS <= r["settled"] < stamp
        ]
        valid = frozenset(
            r["intent_id"] for r in receipts if delivered_text({**r, "settled_at_ms": r["settled"]}) is not None
        )
        original_claims = {c.ref: c for r in receipts for c in frozen[r["intent_id"]]}
        original_claims.update((c.ref, c) for c in head.claims)
        compatible = listing_compatible_links(history, original_claims)
        receipt_models = [
            LinkedReceipt(
                intent_id=r["intent_id"],
                state="sent",
                settled_at_ms=r["settled"],
                claim_refs=tuple(r.get("claim_refs") or (c.ref for c in frozen[r["intent_id"]])),
            )
            for r in receipts
        ]
        active_refs = {ref for c in active.values() for ref in (c.ref, *c.antecedent_refs)}
        first = [link for link in compatible if link.current_ref in active_refs or link.previous_ref in active_refs]
        reached = {ref for link in first for ref in (link.current_ref, link.previous_ref)} - active_refs
        relevant_links = tuple(
            link for link in compatible if link in first or link.current_ref in reached or link.previous_ref in reached
        )
        linked_refs = active_refs | {ref for link in relevant_links for ref in (link.current_ref, link.previous_ref)}
        relevant_receipts = tuple(r for r in receipt_models if set(r.claim_refs) & linked_refs)
        notified = (
            sorted(requested_refs[push])
            if requested_refs is not None
            else [c["claim_ref"] for c in decision["claim_decisions"] if c["decision"] == "notify"]
        )
        if not notified or not set(notified) <= active.keys():
            unknown[push] = "notified_claim_exact_input_missing_or_inactive"
            continue
        for ref in notified:
            claim = active[ref]
            sources = claim_sources(claim, head)
            candidates = tuple(
                Candidate(
                    f"{r['intent_id']}:{key_for(c)}",
                    bundle.vectors[key_for(c)],
                    embedder_key,
                    same_source=bool(index[key_for(c)][1] & sources),
                    group=r["intent_id"],
                )
                for r in receipts
                for c in frozen[r["intent_id"]]
            )
            query = EvaluationQuery(
                f"{decision['notification_id']}:{ref}",
                None,
                script_of(claim.statement),
                Probe(""),
                "",
                Probe(claim.statement, bundle.vectors[key_for(claim)], embedder_key),
                lexical_text(claim),
                (),
                candidates,
                frozenset(),
                frozenset(c.key for c in candidates if c.group in valid),
                {},
                {},
            )
            cases.append(
                ReaderCase(
                    push,
                    ref,
                    query,
                    reader_novelty(ref, compatible, receipt_models),
                    valid,
                    claim,
                    stamp,
                    snapshot.row["analysis_id"],
                    relevant_links,
                    relevant_receipts,
                )
            )
    return cases, unknown


def chain_metrics(
    cases: Sequence[ReaderCase],
    expected: Mapping[str, set[str]],
    unknown: Mapping[str, str],
    calibration: Calibration,
    *,
    degraded: bool = False,
) -> dict[str, Any]:
    selections: dict[str, list[dict[str, Any]]] = defaultdict(list)
    found = set()
    for case in cases:
        selected = reader_selection(case, calibration, degraded=degraded)
        hit = bool(set(selected.intent_ids) & expected[case.push])
        if hit:
            found.add(case.push)
        selections[case.push].append(
            {
                "claim_ref": case.claim_ref,
                "message_intents": list(selected.intent_ids),
                "reasons": [[intent, list(routes)] for intent, routes in selected.reasons],
                "hit": hit,
            }
        )
    return {
        "sf_pushes": len(expected),
        "hits": len(found),
        "unknown": dict(unknown),
        "recall": len(found) / len(expected) if expected else None,
        "misses": sorted(set(expected) - found - unknown.keys()),
        "expected": {k: sorted(v) for k, v in expected.items()},
        "selections": dict(selections),
    }


def reader_selection(case: ReaderCase, calibration: Calibration, *, degraded: bool = False) -> ClaimSelection:
    q = case.query
    probe = Probe(q.receipt_probe.text) if degraded else q.receipt_probe
    prepared = prepare_rank(probe, q.receipt, "receipt", calibration=calibration)
    ranking = rank(prepared, tuple(c for c in q.receipt if c.key in q.receipt_valid))
    return select_for_claim(case.novelty, ranking, available=case.available)


def bind_reader_cases(
    queries: Sequence[EvaluationQuery],
    bundle: VectorBundle,
    facts: Mapping[str, list[dict[str, Any]]],
    original_queries: Sequence[dict[str, Any]],
    index: Mapping[str, tuple[Claim, frozenset[str]]],
) -> None:
    """Receipt acceptance uses the exact recorded reader input and final messages."""
    original = {f"{row['analysis_id']}:{row['slot']}": row for row in original_queries}
    recorded = {d["notification_id"]: d for d in facts["original_audit_decisions"]}
    require(len(recorded) == len(facts["original_audit_decisions"]), "reader_recorded_decision_duplicate")
    decisions, refs = {}, {}
    for q in queries:
        source = original.get(q.key)
        p2 = None if source is None else source.get("p2")
        if p2 is None:
            q.reader_input_error = "recorded_reader_input_missing"
            continue
        decision = recorded.get(p2["notification_id"])
        if decision is not None:
            require(
                decision["started"] == p2["as_of"]
                and decision["event_id"] == bundle.slots[q.key]["event_id"]
                and any(c["claim_ref"] == source["ref"] for c in decision["claim_decisions"]),
                "reader_recorded_decision_binding_mismatch",
            )
        decisions[q.key], refs[q.key] = decision, frozenset({source["ref"]})
    cases, unknown = build_reader_cases(bundle, facts, decisions, index, requested_refs=refs)
    by_key = {q.key: q for q in queries}
    for key, reason in unknown.items():
        by_key[key].reader_input_error = reason
    for case in cases:
        q = by_key[case.push]
        q.receipt_probe, q.receipt_lexical = case.query.receipt_probe, case.query.receipt_lexical
        q.receipt, q.receipt_valid = case.query.receipt, case.query.receipt_valid
        frozen_versions: dict[str, list[str]] = defaultdict(list)
        for candidate in q.receipt:
            frozen_versions[str(candidate.group)].append(candidate.key.removeprefix(str(candidate.group) + ":"))
        q.receipt_labels = receipt_positive_labels(q.prior_labels, q.receipt_labels, frozen_versions)
        q.reader_case = replace(case, query=q)


def reader_target_cases(
    target_path: Path,
    bundle: VectorBundle,
    facts: Mapping[str, list[dict[str, Any]]],
    index: Mapping[str, tuple[Claim, frozenset[str]]],
) -> tuple[list[ReaderCase], dict[str, str], dict[str, dict[str, Any]]]:
    """Actual provisional contexts at the first recorded reader input of each target."""
    targets = read_rows(target_path)
    require(len({r["target_ref"] for r in targets}) == len(targets), "reader_target_ref_duplicate")
    by_analysis = {row["analysis_id"]: row for row in facts["all_snapshots"]}
    decisions, refs, meta = {}, {}, {}
    for target in targets:
        source = by_analysis.get(target["analysis_id"])
        require(
            source is not None
            and all(
                target[field] == source[field]
                for field in ("document", "event_id", "adopted", "completed", "input_revision")
            ),
            "reader_target_exact_snapshot_mismatch",
        )
        head = EventUpdate.model_validate(source["document"])
        ref = target["target_ref"]
        matches = sorted(
            (
                d
                for d in facts["original_audit_decisions"]
                if d["update_ref"] == head.ref and any(c["claim_ref"] == ref for c in d["claim_decisions"])
            ),
            key=lambda d: (d["decided"], d["notification_id"]),
        )
        require(
            not (len(matches) > 1 and matches[0]["decided"] == matches[1]["decided"]),
            "reader_first_decision_order_ambiguous",
        )
        decision = matches[0] if matches else None
        decisions[ref], refs[ref] = decision, frozenset({ref})
        meta[ref] = {
            "target_ref": ref,
            "analysis_id": target["analysis_id"],
            "event_id": target["event_id"],
            "decided_ms": None if decision is None else decision["decided"],
            "as_of_ms": None if decision is None else decision["started"],
            "current_claim": next((c.model_dump(mode="json") for c in head.claims if c.ref == ref), None),
            "evidence": [e.model_dump(mode="json") for e in head.evidence],
        }
    cases, unknown = build_reader_cases(bundle, facts, decisions, index, requested_refs=refs)
    return cases, unknown, meta


def export_reader_contexts(
    cases: Sequence[ReaderCase],
    unknown: Mapping[str, str],
    meta: Mapping[str, dict[str, Any]],
    facts: Mapping[str, list[dict[str, Any]]],
    calibration: Calibration,
) -> list[dict[str, Any]]:
    receipts = {r["intent_id"]: r for r in facts["receipts"]}
    selected_cases = {case.push: case for case in cases}
    result = []
    for ref, value in meta.items():
        case = selected_cases.get(ref)
        selected = () if case is None else reader_selection(case, calibration).intent_ids
        result.append(
            {
                **value,
                "query_analysis_id": None if case is None else case.analysis_id,
                "current_claim": value["current_claim"] if case is None else case.claim.model_dump(mode="json"),
                "status": "unknown" if ref in unknown else "available",
                "unknown_reason": unknown.get(ref),
                "provisional": True,
                "context_origin": "new_actual_shared_core_context_at_original_first_reader_input",
                "embedder": asdict(calibration.embedder),
                "lexical_template": calibration.lexical_template,
                "calibration_digest": calibration.digest,
                "message_intents": list(selected),
                "messages": [
                    {
                        "intent_id": intent,
                        "settled_at_ms": receipts[intent]["settled"],
                        "headline": receipts[intent].get("headline"),
                        "body": receipts[intent]["body"],
                        "payload_sha256": receipts[intent]["payload_sha256"],
                        "sent_claims": receipts[intent]["sent_claims"],
                    }
                    for intent in selected
                ],
                "novelty": None if case is None else case.novelty.novelty,
                "reader_novelty": None if case is None else case.novelty.model_dump(mode="json"),
                "reader_links": [] if case is None else [link.model_dump(mode="json") for link in case.links],
                "linked_receipts": []
                if case is None
                else [
                    {
                        **r.model_dump(mode="json"),
                        "body": receipts[r.intent_id]["body"],
                        "payload_sha256": receipts[r.intent_id]["payload_sha256"],
                        "sent_claims": receipts[r.intent_id]["sent_claims"],
                    }
                    for r in case.linked_receipts
                ],
                "link_path": [] if case is None else [link.model_dump(mode="json") for link in case.novelty.path],
                "first_available_at_ms": case.claim.first_available_at_ms if case is not None else None,
            }
        )
    return result


def attach_lexical_scores(conn: Any, queries: list[EvaluationQuery], index: Mapping[str, tuple[Claim, Any]]) -> None:
    conn.execute(
        "CREATE TEMP TABLE news_claim_index(claim_ref text,text_sha256 text,embed_text text,lexical_text text,"
        "lexical tsvector GENERATED ALWAYS AS (to_tsvector('english',lexical_text)) STORED,"
        "PRIMARY KEY(claim_ref,text_sha256))"
    )
    with conn.cursor().copy("COPY news_claim_index(claim_ref,text_sha256,embed_text,lexical_text) FROM STDIN") as copy:
        for key, (claim, _) in index.items():
            copy.write_row((claim.ref, key.rsplit(":", 1)[1], claim.statement, lexical_text(claim)))
    adapter = ClaimIndexStorage(conn)
    for position, q in enumerate(queries, 1):
        scores = adapter.lexical_scores(
            q.prior_lexical, [(c.key, index[c.key][0]) for c in q.prior if c.key in q.prior_valid]
        )
        q.prior = tuple(replace(c, lexical=scores.get(c.key, 0)) for c in q.prior)
        if q.receipt_probe is not None:
            requested = [
                (c.key, index[c.key.removeprefix(str(c.group) + ":")][0]) for c in q.receipt if c.key in q.receipt_valid
            ]
            scores = adapter.lexical_scores(q.receipt_lexical, requested)
            q.receipt = tuple(replace(c, lexical=scores.get(c.key, 0)) for c in q.receipt)
        if position % 500 == 0:
            print(json.dumps({"stage": "postgres_fts", "queries": position}), flush=True)


def metrics(
    queries: Sequence[EvaluationQuery],
    calibration: Calibration,
    consumer: Consumer,
    *,
    degraded: bool = False,
) -> dict[str, Any]:
    counts: Counter[str] = Counter()
    strata: dict[str, Counter[str]] = defaultdict(Counter)
    selections = {}
    for q in queries:
        probe = q.prior_probe if consumer == "prior" else q.receipt_probe
        labels = q.prior_labels if consumer == "prior" else q.receipt_labels
        if probe is None:
            counts["missing_input_queries"] += 1
            if any(label == "SF" for label in labels.values()):
                counts["sf_queries"] += 1
                counts["missing_sf_input_queries"] += 1
                strata[q.script].update(total=1, success=0)
            continue
        rows = q.prior if consumer == "prior" else q.receipt
        valid = q.prior_valid if consumer == "prior" else q.receipt_valid
        if degraded:
            probe = Probe(probe.text)
        prepared = prepare_rank(probe, rows, consumer, calibration=calibration)
        result = rank(prepared, tuple(c for c in rows if c.key in valid))
        ranked_keys = (
            [h.member_key or h.key for h in result.hits] if consumer == "prior" else [h.key for h in result.hits]
        )
        if consumer == "prior":
            keys = ranked_keys
        elif q.reader_case is None:
            keys = []
            counts["missing_input_queries"] += 1
            counts["missing_sf_input_queries"] += any(label == "SF" for label in labels.values())
        else:
            keys = list(select_for_claim(q.reader_case.novelty, result, available=q.reader_case.available).intent_ids)
        selected_labels = [labels.get(key) for key in keys]
        eligible_gold = {
            c.key for c in rows if c.key in valid and labels.get(c.key if consumer == "prior" else c.group) == "SF"
        }
        if consumer == "receipt" and q.reader_case is not None:
            eligible_gold |= {
                intent
                for intent in q.reader_case.novelty.linked_intents
                if intent in q.reader_case.available and labels.get(intent) == "SF"
            }
        found = any(label == "SF" for label in selected_labels)
        reviewed_gold = any(label == "SF" for label in labels.values())
        baseline_sf_known = (
            q.baseline_sf_known
            if consumer == "prior"
            else any(labels.get(intent) == "SF" for intent in q.receipt_baseline_intents)
        )
        baseline_sf_unknown = (
            q.baseline_sf_unknown
            if consumer == "prior"
            else any(labels.get(intent) is None for intent in q.receipt_baseline_intents)
        )
        counts.update(
            queries=1,
            selected=len(keys),
            unknown_selected=sum(label is None for label in selected_labels),
            labeled_selected=sum(label is not None for label in selected_labels),
            unrelated_selected=sum(label in {"TO", "U"} for label in selected_labels),
            scanned_candidates=result.candidate_count,
            own_baseline=q.own_baseline,
            foreign_baseline=q.foreign_baseline,
            unknown_baseline=q.unknown_baseline,
            reviewed_selected=len(keys) if q.label_id is not None else 0,
            pool_external_selected=sum(label is None for label in selected_labels) if q.label_id is not None else 0,
            unreviewed_selected=len(keys) if q.label_id is None else 0,
            baseline_equivalent_links=q.baseline_equivalent_links,
            baseline_sf_known_queries=int(baseline_sf_known),
            baseline_sf_unknown_queries=int(baseline_sf_unknown and not baseline_sf_known),
            baseline_sf_preserved_queries=int(baseline_sf_known and found),
            raw_ranked_selected=len(ranked_keys),
            raw_ranked_sf_success=int(any(labels.get(key) == "SF" for key in ranked_keys)),
        )
        if q.label_id is not None:
            counts["labeled_queries"] += 1
        if reviewed_gold:
            counts["sf_queries"] += 1
            counts["sf_success"] += found
            strata[q.script].update(total=1, success=int(found))
            counts["eligible_sf_queries"] += bool(eligible_gold)
            counts["sf_gold_outside_scope_queries"] += not eligible_gold
        if consumer == "receipt" and q.receipt_baseline is not None:
            counts["receipt_baseline"] += q.receipt_baseline
        selections[q.key] = {
            "label_query_id": q.label_id,
            "keys": keys,
            "labels": selected_labels,
            "routes": [
                ["semantic"]
                if consumer == "receipt" and q.reader_case is not None and key in q.reader_case.novelty.linked_intents
                else list(
                    next(
                        h.routes
                        for h in result.hits
                        if (h.member_key or h.key if consumer == "prior" else h.key) == key
                    )
                )
                for key in keys
            ],
            "raw_ranked_keys": ranked_keys,
            "reader_input_error": q.reader_input_error,
            "degraded": result.degraded,
        }
    result = dict(counts)
    for key in (
        "selected",
        "unknown_selected",
        "labeled_selected",
        "unrelated_selected",
        "sf_queries",
        "sf_success",
        "foreign_baseline",
        "own_baseline",
        "unknown_baseline",
    ):
        result.setdefault(key, 0)
    result.update(
        sf_success_rate=result["sf_success"] / result["sf_queries"] if result["sf_queries"] else None,
        unrelated_labeled_fraction=result["unrelated_selected"] / result["labeled_selected"]
        if result["labeled_selected"]
        else None,
        unknown_selected_fraction=result["unknown_selected"] / result["selected"] if result["selected"] else None,
        pool_external_selected_fraction=result.get("pool_external_selected", 0) / result["reviewed_selected"]
        if result.get("reviewed_selected")
        else None,
        baseline_sf_preservation_rate=result.get("baseline_sf_preserved_queries", 0)
        / result["baseline_sf_known_queries"]
        if result.get("baseline_sf_known_queries")
        else None,
        strata={k: dict(v) for k, v in strata.items()},
        selections=selections,
    )
    if consumer == "prior":
        result.update(
            foreign_comparison_ratio=result["selected"] / result["foreign_baseline"]
            if result["foreign_baseline"]
            else None,
            total_comparisons=result["selected"] + result["own_baseline"],
            total_baseline=result["own_baseline"] + result["foreign_baseline"],
            total_comparison_ratio=(result["selected"] + result["own_baseline"])
            / (result["own_baseline"] + result["foreign_baseline"])
            if result["own_baseline"] + result["foreign_baseline"]
            else None,
        )
    return result


def acceptance(score: Mapping[str, Any], degraded: Mapping[str, Any], requirements: Mapping[str, Any]) -> list[str]:
    checks = {
        "min_sf_success_rate": (score.get("sf_success_rate"), "min"),
        "max_unrelated_labeled_fraction": (score.get("unrelated_labeled_fraction"), "max"),
        "max_unknown_selected_fraction": (score.get("unknown_selected_fraction"), "max"),
        "max_foreign_comparison_ratio": (score.get("foreign_comparison_ratio"), "max"),
        "max_total_comparison_ratio": (score.get("total_comparison_ratio"), "max"),
        "min_degraded_sf_success_rate": (degraded.get("sf_success_rate"), "min"),
        "min_baseline_sf_preservation": (score.get("baseline_sf_preservation_rate"), "min"),
        "min_degraded_baseline_sf_preservation": (degraded.get("baseline_sf_preservation_rate"), "min"),
    }
    require(
        bool(requirements) and set(requirements) <= checks.keys() | {"min_stratum_sf_success_rate"},
        "fit_acceptance_field_invalid",
    )
    failures = []
    for name, threshold in requirements.items():
        if name == "min_stratum_sf_success_rate":
            require(bool(threshold) and set(threshold) <= {"zh", "ru", "other"}, "fit_stratum_field_invalid")
            for script, floor in threshold.items():
                require(
                    isinstance(floor, (int, float)) and np.isfinite(floor) and 0 <= floor <= 1, "fit_threshold_invalid"
                )
                stat = score["strata"].get(script, {})
                if not stat.get("total") or stat.get("success", 0) / stat["total"] < floor:
                    failures.append(f"stratum:{script}")
            continue
        require(
            isinstance(threshold, (int, float)) and np.isfinite(threshold) and 0 <= threshold <= 1,
            "fit_threshold_invalid",
        )
        value, direction = checks[name]
        if value is None or (value < threshold if direction == "min" else value > threshold):
            failures.append(name)
    return failures


def fit(
    queries: Sequence[EvaluationQuery],
    base: Calibration,
    grid: Mapping[str, Any],
    *,
    chains: tuple[Sequence[ReaderCase], Mapping[str, set[str]], Mapping[str, str]] | None = None,
) -> tuple[Calibration | None, dict[str, Any]]:
    require(grid["version"] == "claim_recall_fit_grid_v1", "fit_grid_version_invalid")
    chosen: dict[str, Cuts] = {}
    reports = {}
    for consumer in ("prior", "receipt"):
        trials = []
        eligible = []
        for raw in grid[consumer]:
            cuts = Cuts(**raw)
            require(
                cuts.k > 0
                and 0 <= cuts.sent_reserved <= cuts.k
                and -1 <= cuts.dense_floor <= 1
                and 0 <= cuts.lexical_floor <= 1
                and 0 <= cuts.degraded_lexical_floor <= 1,
                "fit_cut_invalid",
            )
            candidate = replace(base, **{consumer: cuts})
            score = metrics(queries, candidate, consumer)
            degraded = metrics(queries, candidate, consumer, degraded=True)
            requirements = grid["acceptance"][consumer]
            chain_floor = requirements.get("min_chain_recall")
            require(chain_floor is None or (consumer == "receipt" and chains is not None), "fit_chain_evidence_missing")
            require(bool(requirements), "fit_acceptance_missing")
            other_requirements = {k: v for k, v in requirements.items() if k != "min_chain_recall"}
            failures = acceptance(score, degraded, other_requirements) if other_requirements else []
            chain_score = None if consumer != "receipt" or chains is None else chain_metrics(*chains, candidate)
            if chain_floor is not None and (chain_score["recall"] is None or chain_score["recall"] < chain_floor):
                failures.append("min_chain_recall")
            trials.append(
                {
                    "cuts": asdict(cuts),
                    "policy": candidate.digest,
                    "score": {k: v for k, v in score.items() if k != "selections"},
                    "degraded": {k: v for k, v in degraded.items() if k != "selections"},
                    "failures": failures,
                    "chains": chain_score,
                }
            )
            if not failures:
                eligible.append((cuts, score))
        if eligible:
            cuts, _ = min(
                eligible,
                key=lambda p: (
                    p[1]["selected"],
                    -(p[1]["sf_success_rate"] or 0),
                    p[0].k,
                    p[0].dense_floor,
                    p[0].lexical_floor,
                ),
            )
            chosen[consumer] = cuts
        reports[consumer] = trials
    return (replace(base, **chosen) if len(chosen) == 2 else None), reports


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit-dir", type=Path, required=True)
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--vector-manifest", type=Path, required=True)
    parser.add_argument("--label-provenance", type=Path, required=True)
    parser.add_argument("--window", choices=("original_audit", "business"), default="original_audit")
    parser.add_argument("--postgres-dsn", required=True, help="Isolated evaluation DB; only TEMP tables are written")
    parser.add_argument(
        "--calibration", type=Path, help="Input policy with the selected manifest's exact embedding identity"
    )
    parser.add_argument("--fit-grid", type=Path, help="Explicit Cuts candidates and business acceptance thresholds")
    parser.add_argument(
        "--write-calibration", type=Path, help="Written only if both consumers pass the explicit fit grid"
    )
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--reader-targets", type=Path, help="Exact frozen business target snapshots")
    parser.add_argument(
        "--reader-contexts", type=Path, help="Actual provisional contexts at first recorded reader inputs"
    )
    args = parser.parse_args()
    require(bool(args.fit_grid) == bool(args.write_calibration), "fit_grid_and_output_required_together")
    require(bool(args.reader_targets) == bool(args.reader_contexts), "reader_targets_and_output_required_together")
    calibration = load_calibration(args.calibration)
    bundle = load_vectors(args.vector_manifest, source_manifest=args.source_manifest, calibration=calibration)
    sample, units, provenance = load_labels(args.audit_dir, args.label_provenance)
    facts = load_source_files(args.source_manifest)
    duplicates = load_duplicate_labels(args.audit_dir)
    original_queries = read_json(args.audit_dir / "data/queries_all.json")
    queries, index = build_queries(
        bundle,
        facts,
        sample,
        units,
        original_queries,
        window=args.window,
    )
    bind_reader_cases(queries, bundle, facts, original_queries, index)
    chains = build_chains(bundle, facts, duplicates, index)
    reader_cases, reader_unknown, reader_meta = (
        ([], {}, {})
        if args.reader_targets is None
        else reader_target_cases(
            args.reader_targets,
            bundle,
            facts,
            index,
        )
    )
    with psycopg.connect(args.postgres_dsn, row_factory=dict_row) as conn:
        attach_lexical_scores(conn, [*queries, *(c.query for c in chains[0]), *(c.query for c in reader_cases)], index)
        conn.rollback()
    inputs = {
        "source_manifest": file_sha(args.source_manifest),
        "vector_manifest": bundle.manifest_sha,
        "sample_bound": file_sha(args.audit_dir / "data/sample_bound.json"),
        "units_bound": file_sha(args.audit_dir / "data/units_bound.json"),
        "queries_all": file_sha(args.audit_dir / "data/queries_all.json"),
        "label_provenance": file_sha(args.label_provenance),
        "duplicates_bound": file_sha(args.audit_dir / "data/duplicates_bound.json"),
        "duplicate_binding_manifest": file_sha(args.audit_dir / "duplicate_binding_manifest.json"),
        "duplicate_read_provenance": file_sha(args.audit_dir / "dup_read_provenance.json"),
    }
    dataset = digest(inputs)
    fitted = replace(calibration, dataset_sha256=dataset)
    grid_report = None
    fit_passed = None
    if args.fit_grid:
        grid = read_json(args.fit_grid)
        require(grid["vector_manifest_sha256"] == bundle.manifest_sha, "fit_vector_manifest_digest_mismatch")
        fitted_result, grid_report = fit(queries, fitted, grid, chains=chains)
        fit_passed = fitted_result is not None
        if fitted_result is not None:
            fitted = fitted_result
    report = {
        "version": "claim_recall_replay_v1",
        "dataset_sha256": dataset,
        "input_digests": inputs,
        "embedder": asdict(fitted.embedder),
        "lexical_template": fitted.lexical_template,
        "calibration_sha256": fitted.digest,
        "window": args.window,
        "cuts": {"prior": asdict(fitted.prior), "receipt": asdict(fitted.receipt)},
        "label_pool": provenance,
        "unknown_policy": (
            "Pool-external facts stay unknown except exact reviewed positive claim proof for a frozen receipt; "
            "the complete reviewed pool retains its U contract. Negative claims never judge a full card."
        ),
        "source_scope_unknown_queries": sum(not q.source_scope_known for q in queries),
        "limitations": [
            "Prior replay uses analysis completed_at as an upper bound because the historical p1 call stamp "
            "was not captured; heads adopted during comparison may therefore enter this replay. "
            "Receipt replay uses the exact recorded decision.started/as_of stamp.",
            "Historical sending/ambiguous transitions are unavailable; only immutable sent receipts are candidates.",
            "All checked vectors are available; historical embedding outage/backfill timing is not reconstructed.",
            "Missing frozen input evidence yields no source route; adopted evidence does not substitute input scope.",
            "Receipt primary metrics use final m1..m16, including semantic linked priority; raw rank is diagnostic.",
        ],
        "prior": metrics(queries, fitted, "prior"),
        "receipt": metrics(queries, fitted, "receipt"),
        "degraded_prior": metrics(queries, fitted, "prior", degraded=True),
        "degraded_receipt": metrics(queries, fitted, "receipt", degraded=True),
        "duplicate_chains": chain_metrics(*chains, fitted),
        "degraded_duplicate_chains": chain_metrics(*chains, fitted, degraded=True),
        "fit_grid": grid_report,
        "fit_passed": fit_passed,
    }
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.reader_contexts:
        contexts = export_reader_contexts(reader_cases, reader_unknown, reader_meta, facts, fitted)
        args.reader_contexts.write_text(
            "".join(json.dumps(c, ensure_ascii=False) + "\n" for c in contexts), encoding="utf-8"
        )
    if args.write_calibration:
        require(fit_passed is True, "fit_acceptance_failed_report_written")
        args.write_calibration.write_text(
            json.dumps(asdict(fitted), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    print(
        json.dumps(
            {
                "dataset_sha256": dataset,
                "calibration_sha256": fitted.digest,
                "queries": len(queries),
                "report": str(args.report),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
