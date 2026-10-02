"""Read-only, probability-sampled frozen News reader cases from immutable decisions.

The CLI requires explicit bounds and TRACEFOLD_READONLY_DSN. It reads original
adopted versions and recorded receipt selections, never current heads or recall.
Claim decisions are the sampling units, not independent story representatives.
No provider, judgment cache or sender is constructed.
"""

from __future__ import annotations

import argparse
import json
import os
import random
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import psycopg
from psycopg.rows import dict_row

from scripts.news_reader_io import dataset_sha256, write_jsonl
from tracefold.news.notifications.novelty import ClaimLink, LinkedReceipt, reader_novelty
from tracefold.news.notifications.policy import READER_WAIT_MAX_MS
from tracefold.news.notifications.reader import REPORT_KIND_OPTIONS, ReaderInput
from tracefold.news.updates.contracts import EventUpdate
from tracefold.news.updates.identity import digest, identity

PROTOCOL: Final = "news_reader_case_export_v1"
_READER_REASONS: Final = frozenset(
    {"reader_push", "reader_key", "reader_feed", "reader_ineligible", "reader_unavailable", "reader_unassessed"}
)
_KINDS: Final = frozenset(kind for kind, _ in REPORT_KIND_OPTIONS)

DECISIONS_SQL: Final = """
 SELECT notification_id,event_id,update_ref,decided_at_ms,input_snapshot,plan
 FROM news_notifications
 WHERE kind='update' AND origin='reader_v2' AND decided_at_ms >= %s AND decided_at_ms < %s
 ORDER BY decided_at_ms,notification_id
"""
UPDATES_SQL: Final = """
 SELECT update_ref,event_id,document FROM news_analyses
 WHERE update_ref=ANY(%s) AND adopted_at_ms IS NOT NULL
"""
RECEIPTS_SQL: Final = """
 SELECT notification_id,intent_id,event_id,state,claim_refs,card,attempted_at_ms,settled_at_ms,created_at_ms
 FROM news_notifications WHERE kind='update' AND intent_id IS NOT NULL AND created_at_ms < %s
   AND (intent_id=ANY(%s) OR claim_refs ?| %s::text[])
 ORDER BY intent_id
"""


def decision_frame(decisions: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Name every original claim decision before drawing any sample."""
    frame = []
    for decision in decisions:
        plan = decision["plan"]
        if not isinstance(plan, Mapping) or plan.get("update_ref") != decision["update_ref"]:
            raise ValueError("news_reader_export_decision_plan_mismatch")
        for row in plan["claim_decisions"]:
            record = row.get("reader") or {}
            if not isinstance(record, Mapping):
                raise ValueError("news_reader_export_reader_record_invalid")
            evidence = record.get("judgment") or {}
            kind = evidence.get("report_kind") if isinstance(evidence, Mapping) else None
            kind_value = kind.get("value") if isinstance(kind, Mapping) else None
            kind_value = kind_value if kind_value in _KINDS else "unknown"
            scores = record.get("scores") or {}
            held = scores.get("held") if isinstance(scores, Mapping) else None
            band = (
                "deferred"
                if row["decision"] == "deferred"
                else "key"
                if row["reason"] == "reader_key"
                else "push"
                if row["decision"] == "notify"
                else "held"
                if held
                else "feed"
            )
            frame.append(
                {
                    "case_id": identity("news_reader_case", decision["notification_id"], row["claim_ref"]),
                    "decision": dict(decision),
                    "row": dict(row),
                    "decision_band": band,
                    "report_kind_stratum": kind_value,
                    "stratum": f"{band}/{kind_value}",
                }
            )
    if not frame or len({row["case_id"] for row in frame}) != len(frame):
        raise ValueError("news_reader_export_empty_or_duplicate_frame")
    return sorted(frame, key=lambda row: row["case_id"])


def sample_frame(
    frame: Sequence[Mapping[str, Any]],
    *,
    from_ms: int,
    to_ms: int,
    per_stratum: int,
    key_per_stratum: int,
    seed: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not 0 <= from_ms < to_ms or min(per_stratum, key_per_stratum) < 1:
        raise ValueError("news_reader_export_sample_arguments_invalid")
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in frame:
        groups[row["stratum"]].append(row)
    frame_id = identity(
        "news_reader_sample_frame",
        from_ms,
        to_ms,
        [
            (row["case_id"], row["stratum"], digest(row["decision"]["plan"]))
            for row in sorted(frame, key=lambda row: row["case_id"])
        ],
    )
    start = datetime.fromtimestamp(from_ms / 1000, UTC).date().toordinal()
    end = datetime.fromtimestamp((to_ms - 1) / 1000, UTC).date().toordinal()
    days = [datetime.fromordinal(day).date().isoformat() for day in range(start, end + 1)]
    sampling_frame = {
        "frame_id": frame_id,
        "unit": "claim_decision",
        "units": len(frame),
        "stratum_sizes": {key: len(value) for key, value in sorted(groups.items())},
        "days": days,
        "certification_strata": sorted(groups),
    }
    chosen: list[dict[str, Any]] = []
    rng = random.Random(seed)  # noqa: S311 -- reproducible probability sampling, no security use
    sample_counts = {}
    for stratum, candidates in sorted(groups.items()):
        limit = key_per_stratum if stratum.startswith("key/") else per_stratum
        n = min(limit, len(candidates))
        sample_counts[stratum] = n
        chosen.extend(
            dict(row)
            | {
                "sampling_design": "stratified",
                "sampling_unit": "claim_decision",
                "sampling_frame": sampling_frame,
                "inclusion_probability": n / len(candidates),
            }
            for row in rng.sample(sorted(candidates, key=lambda row: row["case_id"]), n)
        )
    manifest = {
        "protocol": PROTOCOL,
        "from_ms": from_ms,
        "to_ms": to_ms,
        "sampling_frame": sampling_frame,
        "sample_counts": sample_counts,
        "seed": seed,
        "per_stratum": per_stratum,
        "key_per_stratum": key_per_stratum,
        "selection": "uniform without replacement within each frozen decision-band/report-kind stratum",
        "story_ids_known": False,
        "certification_ready": False,
        "owner_sampling_requirement": "independent story representative frame and final joint inclusion probability",
        "historical_held_requirement": "held stratum requires recorded scores.held; historical values are not inferred",
    }
    return sorted(chosen, key=lambda row: row["case_id"]), manifest


def receipt_as_of(row: Mapping[str, Any], at_ms: int) -> LinkedReceipt | None:
    """Recover provable delivery facts; retained retry rows are not an attempt log.

    A pending retry can retain an earlier attempted_at while clearing settled_at.
    A later attempt also overwrites attempted_at. Neither proves its historical
    state at a cutoff. Reject such intervals instead of inventing in-flight
    evidence or silently omitting a potentially relevant earlier attempt.
    """
    if row["created_at_ms"] > at_ms:
        return None
    settled = row.get("settled_at_ms")
    attempted = row.get("attempted_at_ms")
    if attempted is not None and settled is not None and attempted > settled:
        raise ValueError("news_reader_export_receipt_history_unverifiable")
    if settled is not None and settled <= at_ms:
        if row["state"] not in {"sent", "ambiguous"}:
            return None
        state, received = row["state"], settled
    elif (
        attempted is not None
        and attempted <= at_ms
        and (row["state"] == "sending" or (row["state"] in {"sent", "ambiguous", "terminal"} and settled is not None))
    ):
        state, received = "sending", None
    elif attempted is None and row["state"] in {"pending", "dead"}:
        return None
    else:
        raise ValueError("news_reader_export_receipt_history_unverifiable")
    return LinkedReceipt(
        intent_id=row["intent_id"],
        state=state,
        claim_refs=tuple(row.get("claim_refs") or ()),
        settled_at_ms=received,
    )


def restore_case(
    selected: Mapping[str, Any],
    updates: Mapping[str, Mapping[str, Any]],
    receipts: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    decision, row = selected["decision"], selected["row"]
    raw_update = updates.get(decision["update_ref"])
    if raw_update is None or raw_update["event_id"] != decision["event_id"]:
        raise ValueError("news_reader_export_original_update_missing")
    update = EventUpdate.model_validate(raw_update["document"])
    if update.ref != decision["update_ref"] or update.event_id != decision["event_id"]:
        raise ValueError("news_reader_export_original_update_mismatch")
    claim = next((claim for claim in update.claims if claim.ref == row["claim_ref"]), None)
    if claim is None:
        raise ValueError("news_reader_export_original_claim_missing")
    at_ms = int(decision["decided_at_ms"])
    record = row.get("reader") or {}
    intents = list(record.get("message_intents", ()))
    if len(intents) != len(set(intents)):
        raise ValueError("news_reader_export_duplicate_message_intent")
    by_id = {receipt["intent_id"]: receipt for receipt in receipts}
    compared = {
        receipt["intent_id"]: receipt["payload_sha256"] for receipt in decision["plan"].get("compared_receipts", ())
    }
    messages, message_hashes = [], []
    for intent in intents:
        receipt = by_id.get(intent)
        native = None if receipt is None else receipt_as_of(receipt, at_ms)
        if receipt is None or native is None or native.state != "sent":
            raise ValueError("news_reader_export_original_message_missing")
        card = receipt.get("card") or {}
        body, payload = card.get("body"), card.get("payload_sha256")
        if not isinstance(body, str) or digest(body) != payload or compared.get(intent) != payload:
            raise ValueError("news_reader_export_original_payload_mismatch")
        messages.append(body)
        message_hashes.append(payload)
    frozen = ReaderInput.of(claim, update, messages)
    applicable = row["reason"] in _READER_REASONS
    original_digest = record.get("input_digest")
    if original_digest is not None and original_digest != frozen.digest:
        raise ValueError("news_reader_export_original_input_mismatch")
    if applicable and original_digest is None:
        raise ValueError("news_reader_export_original_input_unverifiable")
    links = tuple(ClaimLink.model_validate(link) for link in record.get("link_path", ()))
    if any(link.asserted_at_ms > at_ms for link in links):
        raise ValueError("news_reader_export_future_link")
    path_refs = {claim.ref} | {ref for link in links for ref in (link.current_ref, link.previous_ref)}
    linked = tuple(
        native
        for receipt in receipts
        if receipt.get("notification_id") != decision["notification_id"]
        and set(receipt.get("claim_refs") or ()) & path_refs
        and (native := receipt_as_of(receipt, at_ms)) is not None
    )
    novelty = reader_novelty(claim.ref, links, linked)
    if record.get("novelty") is not None and record["novelty"] != novelty.novelty:
        raise ValueError("news_reader_export_original_novelty_mismatch")
    pre_reason = (
        ("reader_unassessed" if at_ms - update.adopted_at_ms > READER_WAIT_MAX_MS else "reader_unavailable")
        if applicable
        else row["reason"]
    )
    return {
        "case_id": selected["case_id"],
        "claim_ref": claim.ref,
        "event_id": update.event_id,
        "decision_ref": decision["notification_id"],
        "update_ref": update.ref,
        "content_revision": update.content_revision,
        "decided_at_ms": at_ms,
        "first_available_at_ms": claim.first_available_at_ms,
        "reader_input": frozen.model_dump(mode="json"),
        "reader_input_sha256": frozen.digest,
        "recorded_input_sha256": original_digest,
        "reader_input_provenance": "verified_original" if original_digest is not None else "derived_unasked",
        "source_document_sha256": digest(raw_update["document"]),
        "message_intents": intents,
        "message_payload_sha256": message_hashes,
        "links": [link.model_dump(mode="json") for link in links],
        "receipts": [receipt.model_dump(mode="json") for receipt in linked],
        "novelty": novelty.model_dump(mode="json"),
        "reader_revision": decision["plan"]["reader_revision"],
        "reader_identity": decision["plan"]["reader_identity"],
        "original_reason": row["reason"],
        "original_decision": row["decision"],
        "reader_applicable": applicable,
        "pre_reader_reason": pre_reason,
        "deterministic_decision": None
        if applicable
        else {"notify": "notify", "not_notified": "drop", "deferred": "deferred"}[row["decision"]],
        "decision_band": selected["decision_band"],
        "report_kind_stratum": selected["report_kind_stratum"],
        "type_stratum_source": "unknown"
        if selected["report_kind_stratum"] == "unknown"
        else "stored_reader_report_kind",
        **{
            key: selected[key]
            for key in ("stratum", "sampling_design", "sampling_unit", "sampling_frame", "inclusion_probability")
        },
    }


def export_cases(
    conn: Any,
    *,
    from_ms: int,
    to_ms: int,
    per_stratum: int = 50,
    key_per_stratum: int = 100,
    seed: int = 805,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    frame = decision_frame(conn.execute(DECISIONS_SQL, (from_ms, to_ms)).fetchall())
    selected, manifest = sample_frame(
        frame,
        from_ms=from_ms,
        to_ms=to_ms,
        per_stratum=per_stratum,
        key_per_stratum=key_per_stratum,
        seed=seed,
    )
    update_refs = sorted({row["decision"]["update_ref"] for row in selected})
    updates = {row["update_ref"]: row for row in conn.execute(UPDATES_SQL, (update_refs,)).fetchall()}
    intent_ids, claim_refs = set(), set()
    for chosen in selected:
        record = chosen["row"].get("reader") or {}
        intent_ids.update(record.get("message_intents", ()))
        claim_refs.add(chosen["row"]["claim_ref"])
        for link in record.get("link_path", ()):
            claim_refs.update((link["current_ref"], link["previous_ref"]))
    receipts = conn.execute(RECEIPTS_SQL, (to_ms, sorted(intent_ids), sorted(claim_refs))).fetchall()
    rows = [restore_case(row, updates, receipts) for row in selected]
    manifest.update(
        selected_units=len(rows),
        dataset_sha256=dataset_sha256(rows),
        input_provenance_counts=dict(Counter(row["reader_input_provenance"] for row in rows)),
        reader_applicable_units=sum(row["reader_applicable"] for row in rows),
        deterministic_units=sum(not row["reader_applicable"] for row in rows),
    )
    return rows, manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from-ms", type=int, required=True)
    parser.add_argument("--to-ms", type=int, required=True)
    parser.add_argument("--per-stratum", type=int, default=50)
    parser.add_argument("--key-per-stratum", type=int, default=100)
    parser.add_argument("--seed", type=int, default=805)
    parser.add_argument("--output", type=Path, required=True, help="JSONL.gz frozen cases")
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    if not 0 <= args.from_ms < args.to_ms or min(args.per_stratum, args.key_per_stratum) < 1:
        parser.error("positive sample counts and an explicit nonempty time interval are required")
    if not str(args.output).endswith(".jsonl.gz") or args.output.resolve() == args.manifest.resolve():
        parser.error("output must be JSONL.gz and manifest must be a separate path")
    with (
        psycopg.connect(
            os.environ["TRACEFOLD_READONLY_DSN"],
            options="-c default_transaction_read_only=on -c statement_timeout=30000",
            row_factory=dict_row,
        ) as conn,
        conn.transaction(),
    ):
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        rows, manifest = export_cases(
            conn,
            from_ms=args.from_ms,
            to_ms=args.to_ms,
            per_stratum=args.per_stratum,
            key_per_stratum=args.key_per_stratum,
            seed=args.seed,
        )
    if write_jsonl(args.output, rows) != manifest["dataset_sha256"]:
        raise ValueError("news_reader_export_dataset_digest_mismatch")
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    with args.manifest.open("w", encoding="utf-8") as stream:
        args.manifest.chmod(0o600)
        stream.write(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
