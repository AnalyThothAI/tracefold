"""Read-only #750 equivalence audit and active-link refresh dry run.

Uses persisted claim versions at each relation assertion. This cannot re-ask the relation model or
reconstruct unpublished historical provider states; those are reported as unverified, not inferred.
"""

from __future__ import annotations

import argparse
import json
from bisect import bisect_right
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from tracefold.news.storage.root import NewsRepository
from tracefold.news.updates.contracts import Claim, DraftClaim
from tracefold.news.updates.semantics import proven_mismatches
from tracefold.platform.config.loader import load_settings
from tracefold.platform.postgres.client import with_password_from_file

DAY_MS = 86_400_000


def _free_text_differences(current: Claim, previous: Claim) -> tuple[str, ...]:
    a, b = current.fields, previous.fields
    fields = []
    if (
        a.conditions
        and b.conditions
        and {value.casefold().strip() for value in a.conditions} != {value.casefold().strip() for value in b.conditions}
    ):
        fields.append("conditions")
    for key in ("statistical_period", "effective_at", "occurred_at"):
        left, right = getattr(a, key), getattr(b, key)
        if left and right and left.casefold().strip() != right.casefold().strip():
            fields.append(key)
    return tuple(fields)


def _other_historical_veto(current: Claim, previous: Claim) -> bool:
    """Exclude pairs whose archived veto had a separate identity, enum or numeric reason."""

    a, b = current.fields, previous.fields
    current_ids: dict[str, set[str]] = defaultdict(set)
    previous_ids: dict[str, set[str]] = defaultdict(set)
    for hint in current.known_identity:
        current_ids[hint.key].add(hint.value)
    for hint in previous.known_identity:
        previous_ids[hint.key].add(hint.value)
    if any(current_ids[key] != previous_ids[key] for key in current_ids.keys() & previous_ids.keys()):
        return True
    if "unknown" not in {a.polarity, b.polarity} and a.polarity != b.polarity:
        return True
    if "unknown" not in {a.mode, b.mode} and a.mode != b.mode:
        return True
    if a.phase not in {None, "unknown"} and b.phase not in {None, "unknown"} and a.phase != b.phase:
        return True
    qa = {(q.name.casefold(), q.unit.casefold(), q.period): Decimal(q.value) for q in a.quantities}
    qb = {(q.name.casefold(), q.unit.casefold(), q.period): Decimal(q.value) for q in b.quantities}
    return any(qa[key] != qb[key] for key in qa.keys() & qb.keys())


def audit(conn: psycopg.Connection[Any], *, as_of_ms: int, days: int = 7) -> dict[str, Any]:
    """Compare archived text vetoes with current proof, without changing any ledger row."""

    conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
    updates = conn.execute(
        "SELECT event_id, adopted_at_ms, document FROM news_event_updates WHERE adopted_at_ms < %s",
        (as_of_ms,),
    ).fetchall()
    links = conn.execute(
        """SELECT current_event_id,previous_event_id,current_ref,previous_ref,relation,asserted_at_ms
             FROM news_claim_links WHERE asserted_at_ms >= %s AND asserted_at_ms < %s
             ORDER BY asserted_at_ms,current_ref,previous_ref""",
        (as_of_ms - days * DAY_MS, as_of_ms),
    ).fetchall()
    versions: dict[tuple[str, str], list[tuple[int, Claim]]] = defaultdict(list)
    for update in updates:
        for raw in update["document"].get("claims", ()):
            claim = Claim.model_validate(raw)
            versions[(update["event_id"], claim.ref)].append((int(update["adopted_at_ms"]), claim))
    for rows in versions.values():
        rows.sort(key=lambda item: item[0])

    def at(event_id: str, ref: str, stamp: int) -> Claim | None:
        rows = versions.get((event_id, ref), ())
        position = bisect_right([item[0] for item in rows], stamp) - 1
        return rows[position][1] if position >= 0 else None

    affected = []
    text_different = 0
    missing = 0
    for link in links:
        stamp = int(link["asserted_at_ms"])
        current = at(link["current_event_id"], link["current_ref"], stamp)
        previous = at(link["previous_event_id"], link["previous_ref"], stamp)
        if current is None or previous is None:
            missing += 1
            continue
        text_fields = _free_text_differences(current, previous)
        if not text_fields:
            continue
        text_different += 1
        if _other_historical_veto(current, previous):
            continue
        draft = DraftClaim(
            slot="audit",
            statement=current.statement,
            fields=current.fields,
            topics=current.topics,
            citations=current.citations,
        )
        proven = proven_mismatches(draft, previous, current.known_identity)
        affected.append(
            {
                "current_event_id": link["current_event_id"],
                "current_ref": link["current_ref"],
                "previous_ref": link["previous_ref"],
                "relation": link["relation"],
                "asserted_at_ms": stamp,
                "old_free_text_fields": text_fields,
                "proven_mismatches": proven,
            }
        )
    latest_assertion = {
        frozenset((row["current_ref"], row["previous_ref"])): (
            row["current_ref"],
            row["previous_ref"],
            int(row["asserted_at_ms"]),
        )
        for row in links
    }
    active = sorted(
        (
            row
            for row in affected
            if row["asserted_at_ms"] >= as_of_ms - 2 * DAY_MS
            and not row["proven_mismatches"]
            and latest_assertion[frozenset((row["current_ref"], row["previous_ref"]))]
            == (row["current_ref"], row["previous_ref"], row["asserted_at_ms"])
        ),
        key=lambda row: (row["current_event_id"], row["current_ref"], row["previous_ref"]),
    )
    # Use the same read-only inspection endpoint the operator's `news reanalyze` preview uses.
    # A historical affected link is not necessarily still in the current head. Only a completed
    # read that cites its current claim can be proposed for a fresh relation assertion.
    storage = NewsRepository(conn)
    refresh_scopes = []
    for event_id in sorted({row["current_event_id"] for row in active}):
        refs = {row["current_ref"] for row in active if row["current_event_id"] == event_id}
        head = storage.event_update_head_document(event_id)
        if head is None:
            refresh_scopes.append({"event_id": event_id, "status": "head_missing"})
            continue
        citations = {
            citation["evidence_ref"]
            for claim in head.get("claims", ())
            if claim["ref"] in refs
            for citation in claim.get("citations", ())
        }
        if not citations:
            refresh_scopes.append({"event_id": event_id, "status": "claim_not_current"})
            continue
        try:
            listing = storage.reanalysis_scope_list(event_id=event_id, now_ms=as_of_ms)
        except (LookupError, ValueError) as exc:
            refresh_scopes.append({"event_id": event_id, "status": str(exc)})
            continue
        matches = [scope for scope in listing["scopes"] if scope["evidence_ref"] in citations]
        status = (
            "ready"
            if matches and (listing["done_revision"] == listing["wanted_revision"] or listing["failed"])
            else "scope_or_work_not_ready"
        )
        refresh_scopes.append(
            {
                "event_id": event_id,
                "status": status,
                "wanted_revision": listing["wanted_revision"],
                "head_revision": listing["head_revision"],
                "claim_refs": sorted(refs),
                "read_refs": sorted({scope["read_ref"] for scope in matches}),
            }
        )
    return {
        "as_of_ms": as_of_ms,
        "window_days": days,
        "links_examined": len(links),
        "missing_historical_claim_version": missing,
        "free_text_different_pairs": text_different,
        "text_only_historical_veto_pairs": len(affected),
        "still_proven_mismatch": sum(bool(row["proven_mismatches"]) for row in affected),
        "now_unknown": sum(not row["proven_mismatches"] for row in affected),
        "relation_distribution": dict(Counter(row["relation"] for row in affected)),
        "active_refresh_targets": active,
        "active_refresh_target_events": sorted({row["current_event_id"] for row in active}),
        "active_refresh_target_count": len(active),
        "active_refresh_target_event_count": len({row["current_event_id"] for row in active}),
        "refresh_scope_dry_run": refresh_scopes,
        "refresh_ready_events": sum(row["status"] == "ready" for row in refresh_scopes),
        "model_relation_changed": "unverified; requires reanalysis with the new question",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of-ms", type=int, required=True)
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--host", help="optional read-only connection host override")
    parser.add_argument("--port", type=int, help="optional read-only connection port override")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    settings = load_settings(require_ws_token=False)
    conninfo = psycopg.conninfo.conninfo_to_dict(
        with_password_from_file(settings.storage.postgres.dsn, settings.postgres_password_file())
    )
    conninfo.update(options="-c default_transaction_read_only=on", row_factory=dict_row)
    if args.host:
        conninfo["host"] = args.host
    if args.port:
        conninfo["port"] = args.port
    with psycopg.connect(**conninfo) as conn:
        report = audit(conn, as_of_ms=args.as_of_ms, days=args.days)
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
        print(
            {
                key: value
                for key, value in report.items()
                if key not in {"active_refresh_targets", "active_refresh_target_events", "refresh_scope_dry_run"}
            }
        )
    else:
        print(rendered, end="")


if __name__ == "__main__":
    main()
