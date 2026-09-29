"""Read-only freeze of the Nvidia and SpaceX #750 event sequences under the new reader context."""

from __future__ import annotations

import argparse
import gzip
import json
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from tracefold.news.storage.event_updates import EventUpdateStorage, delivered_text
from tracefold.news.updates.contracts import EventUpdate
from tracefold.news.updates.reader_judgments import ClaimLink, LinkedReceipt, ReaderInput, reader_novelty
from tracefold.platform.config.loader import load_settings
from tracefold.platform.postgres.client import with_password_from_file

FIXTURES = Path(__file__).resolve().parents[1] / "tests/fixtures/news"
CLUSTERS = ("reader_nvidia_buyback_2026-09-28.jsonl", "reader_spacex_starship_2026-09-28.jsonl")


def freeze(conn: psycopg.Connection[Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
    archived = [json.loads(line) for name in CLUSTERS for line in (FIXTURES / name).read_text("utf-8").splitlines()]
    versions = conn.execute("SELECT event_id,document FROM news_event_updates").fetchall()
    by_ref = {EventUpdate.model_validate(row["document"]).ref: row["document"] for row in versions}
    decisions = conn.execute(
        "SELECT event_id,update_ref,created_at_ms,plan FROM news_notification_decisions"
    ).fetchall()
    by_decision = {(row["event_id"], int(row["created_at_ms"])): row for row in decisions}
    storage = EventUpdateStorage()
    storage.conn = conn
    frozen = []
    skipped = []
    for case in archived:
        event_id, stamp, ref = case["event_id"], int(case["decided_at_ms"]), case["claim_ref"]
        decision = by_decision.get((event_id, stamp))
        if decision is None or decision["update_ref"] not in by_ref:
            skipped.append((case["case_id"], "missing_decision_or_version"))
            continue
        update = EventUpdate.model_validate(by_ref[decision["update_ref"]])
        claim = next((claim for claim in update.claims if claim.ref == ref), None)
        if claim is None:
            skipped.append((case["case_id"], "claim_not_in_decision_version"))
            continue
        context = storage._reader_state(event_id=event_id, head=update, now_ms=stamp, watch_symbols=())
        if ref not in context["receipt_intents_by_claim"]:
            if case["production_reason"] == "retired":
                frozen.append(
                    {
                        "case_id": case["case_id"],
                        "cluster": case["case_id"].split(":")[0],
                        "event_id": event_id,
                        "claim_ref": ref,
                        "as_of_ms": stamp,
                        "deterministic_outcome": "retired",
                    }
                )
            else:
                skipped.append((case["case_id"], "inactive_at_replay"))
            continue
        selection = list(context["receipt_intents_by_claim"][ref])
        bodies = {
            str(row["intent_id"]): text.body for row in context["receipts"] if (text := delivered_text(row)) is not None
        }
        if any(intent not in bodies for intent in selection):
            raise ValueError(f"news_reader_cluster_missing_sent_body:{case['case_id']}")
        links = tuple(
            ClaimLink(
                current_ref=str(row["current_ref"]),
                previous_ref=str(row["previous_ref"]),
                relation=row["relation"],
                asserted_at_ms=int(row["asserted_at_ms"]),
            )
            for row in context["links"]
        )
        receipts = tuple(
            LinkedReceipt(
                intent_id=str(row["intent_id"]),
                state=row["state"],
                claim_refs=tuple(str(item) for item in row["claim_refs"] or ()),
                settled_at_ms=None if row["settled_at_ms"] is None else int(row["settled_at_ms"]),
            )
            for row in context["linked"]
        )
        reader = ReaderInput.of(claim, update, [bodies[intent] for intent in selection])
        novelty = reader_novelty(ref, links, receipts)
        frozen.append(
            {
                "case_id": case["case_id"],
                "cluster": case["case_id"].split(":")[0],
                "event_id": event_id,
                "claim_ref": ref,
                "as_of_ms": stamp,
                "first_available_at_ms": case["first_available_at_ms"],
                "production_reason": case["production_reason"],
                "baseline_messages": len(case["message_intents"]),
                "baseline_answers": case.get("answers", {}),
                "novelty": novelty.model_dump(mode="json"),
                "message_intents": selection,
                "reader_input": reader.model_dump(mode="json"),
                "input_digest": reader.digest,
            }
        )
    return frozen, {"archived": len(archived), "frozen": len(frozen), "skipped": skipped}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--output", type=Path, required=True)
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
    with psycopg.connect(**params) as conn:
        rows, report = freeze(conn)
    with gzip.open(args.output, "wt", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
