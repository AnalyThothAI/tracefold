"""Freeze a stratified #750 reader replay from archived independent labels and read-only PG.

The archived case IDs abbreviate claim refs. Match a claim by its full statement and first
availability, then require exactly one persisted decision carrying that claim. Ambiguous matches
are excluded rather than assigning a guessed decision stamp. This is an offline export only.
"""

from __future__ import annotations

import argparse
import gzip
import json
import random
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from tracefold.news.storage.event_updates import EventUpdateStorage, delivered_text
from tracefold.news.updates.contracts import Claim, EventUpdate
from tracefold.news.updates.identity import identity
from tracefold.news.updates.reader_judgments import ClaimLink, LinkedReceipt, ReaderInput, reader_novelty
from tracefold.news.updates.receipt_recall import RecallCandidate, query_for_claim, select_for_claim
from tracefold.platform.config.loader import load_settings
from tracefold.platform.postgres.client import with_password_from_file

BASELINE = Path(__file__).resolve().parents[1] / "tests/fixtures/news/reader_replay_2026-09-28.jsonl.gz"


def build(
    conn: psycopg.Connection[Any], *, count: int, case_ids: tuple[str, ...] = ()
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
    with gzip.open(BASELINE, "rt", encoding="utf-8") as source:
        archived = [json.loads(line) for line in source]
    versions = conn.execute(
        "SELECT event_id,content_revision,adopted_at_ms,document FROM news_event_updates"
    ).fetchall()
    decisions = conn.execute(
        "SELECT event_id,update_ref,created_at_ms,plan FROM news_notification_decisions"
    ).fetchall()
    by_statement: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    by_update = {identity("update", row["event_id"], row["content_revision"]): row for row in versions}
    for version in versions:
        for claim in version["document"].get("claims", ()):
            by_statement[(claim["statement"], int(claim["first_available_at_ms"]))].append(
                {"event_id": version["event_id"], "claim_ref": claim["ref"]}
            )
    by_claim: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for decision in decisions:
        for claim_decision in decision["plan"].get("claim_decisions", ()):
            by_claim[(decision["event_id"], claim_decision["claim_ref"])].append(decision)
    eligible = []
    skipped: Counter[str] = Counter()
    for case in archived:
        statement = case["reader_input"]["claim"]["statement"]
        possible = {
            (match["event_id"], match["claim_ref"])
            for match in by_statement[(statement, int(case["first_available_at_ms"]))]
        }
        matched = {
            (event, ref, row["update_ref"], row["created_at_ms"]): row
            for event, ref in possible
            for row in by_claim[(event, ref)]
        }
        if len(matched) != 1:
            skipped["ambiguous_decision" if matched else "missing_decision"] += 1
            continue
        (event, ref, update_ref, stamp), decision = next(iter(matched.items()))
        version = by_update.get(str(update_ref))
        if version is None or version["event_id"] != event:
            skipped["missing_update_version"] += 1
            continue
        update = EventUpdate.model_validate(version["document"])
        if not any(claim.ref == ref for claim in update.claims):
            skipped["claim_not_in_decision_version"] += 1
            continue
        eligible.append((case, update, ref, int(stamp)))
    # All cases form validation, with no policy tuning on their labels. Spread the rare keep and
    # borderline cases across novelty/language, then fill with demotes from distinct event families.
    if case_ids:
        position = {case_id: index for index, case_id in enumerate(case_ids)}
        eligible = [item for item in eligible if item[0]["case_id"] in position]
        eligible.sort(key=lambda item: position[item[0]["case_id"]])
    else:
        rng = random.Random(750)  # noqa: S311 - deterministic offline sampling, not a security decision
        rng.shuffle(eligible)
        eligible.sort(key=lambda item: {"keep": 0, "borderline": 1, "demote": 2}[item[0]["label"]["verdict"]])
    seen_events = set()
    storage = EventUpdateStorage()
    storage.conn = conn
    output = []
    for case, update, ref, stamp in eligible:
        if update.event_id in seen_events:
            continue
        seen_events.add(update.event_id)
        claim = next(claim for claim in update.claims if claim.ref == ref)
        context = storage._reader_state(event_id=update.event_id, head=update, now_ms=stamp, watch_symbols=())
        if ref not in context["receipt_intents_by_claim"]:
            skipped["inactive_at_replay"] += 1
            continue
        links = tuple(
            ClaimLink(
                current_ref=str(row["current_ref"]),
                previous_ref=str(row["previous_ref"]),
                relation=row["relation"],
                asserted_at_ms=int(row["asserted_at_ms"]),
            )
            for row in context["links"]
        )
        linked = tuple(
            LinkedReceipt(
                intent_id=str(row["intent_id"]),
                state=row["state"],
                claim_refs=tuple(str(item) for item in row["claim_refs"] or ()),
                settled_at_ms=None if row["settled_at_ms"] is None else int(row["settled_at_ms"]),
            )
            for row in context["linked"]
        )
        novelty = reader_novelty(ref, links, linked)
        ordinary = storage._recall_receipt_rows((query_for_claim(claim),), now_ms=stamp)
        allowed = {str(row["intent_id"]) for row in ordinary} | set(novelty.linked_intents)
        by_intent = {str(row["intent_id"]): row for row in (*context["linked"], *ordinary)}
        candidates = tuple(
            RecallCandidate(
                intent_id=intent,
                body=text.body,
                payload_sha256=text.payload_sha256,
                settled_at_ms=int(text.received_at_ms),
                claims=tuple(Claim.model_validate(raw) for raw in row.get("historical_claims") or ()),
            )
            for intent, row in by_intent.items()
            if intent in allowed
            and (text := delivered_text(row)) is not None
            and text.state == "sent"
            and text.received_at_ms is not None
            and text.received_at_ms < stamp
        )
        selection = select_for_claim(
            query_for_claim(claim),
            novelty,
            candidates,
            as_of_ms=stamp,
            route_ranks={str(row["intent_id"]): (row["structure_rank"], row["lexical_rank"]) for row in ordinary},
        )
        if selection.intent_ids != tuple(context["receipt_intents_by_claim"][ref]):
            raise ValueError(f"news_reader_replay_selection_drift:{case['case_id']}")
        body_by_intent = {row.intent_id: row.body for row in candidates}
        reader = ReaderInput.of(claim, update, [body_by_intent[intent] for intent in selection.intent_ids])
        output.append(
            {
                "case_id": case["case_id"],
                "event_id": update.event_id,
                "claim_ref": ref,
                "as_of_ms": stamp,
                "first_available_at_ms": case["first_available_at_ms"],
                "label": case["label"],
                "stratum": case["stratum"],
                "weight": case["weight"],
                "baseline_messages": len(case["message_intents"]),
                "baseline_answers": case.get("answers", {}),
                "current_claim": claim.model_dump(mode="json"),
                "novelty": novelty.model_dump(mode="json"),
                "links": [row.model_dump(mode="json") for row in links],
                "link_receipts": [row.model_dump(mode="json") for row in linked],
                "candidates": [
                    {
                        "intent_id": row.intent_id,
                        "body": row.body,
                        "payload_sha256": row.payload_sha256,
                        "settled_at_ms": row.settled_at_ms,
                        "claims": [claim.model_dump(mode="json") for claim in row.claims],
                    }
                    for row in candidates
                ],
                "route_ranks": {
                    str(row["intent_id"]): [row["structure_rank"], row["lexical_rank"]] for row in ordinary
                },
                "message_intents": list(selection.intent_ids),
                "selection_reasons": dict(selection.reasons),
                "reader_input": reader.model_dump(mode="json"),
                "input_digest": reader.digest,
            }
        )
        if len(output) == count:
            break
    report = {
        "archive_cases": len(archived),
        "eligible_unique_decision": len(eligible),
        "selected": len(output),
        "skipped": dict(skipped),
        "label_verdicts": dict(Counter(row["label"]["verdict"] for row in output)),
        "novelty": dict(Counter(row["novelty"]["novelty"] for row in output)),
        "source": "2026-09-28 independent labels matched to read-only frozen PG versions",
    }
    return output, report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=120)
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--case-ids-from", type=Path, help="rebuild the same cases after a replay code correction")
    args = parser.parse_args()
    settings = load_settings(require_ws_token=False)
    params = psycopg.conninfo.conninfo_to_dict(
        with_password_from_file(settings.storage.postgres.dsn, settings.postgres_password_file())
    )
    params.update(options="-c default_transaction_read_only=on", row_factory=dict_row)
    if args.host:
        params["host"] = args.host
    if args.port:
        params["port"] = args.port
    case_ids = ()
    if args.case_ids_from:
        with gzip.open(args.case_ids_from, "rt", encoding="utf-8") as source:
            case_ids = tuple(json.loads(line)["case_id"] for line in source)
    with psycopg.connect(**params) as conn:
        rows, report = build(conn, count=len(case_ids) if case_ids else args.count, case_ids=case_ids)
    with gzip.open(args.output, "wt", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
