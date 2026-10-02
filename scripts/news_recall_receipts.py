"""Daily read-only News recall statistics and a dense-similarity missed-recall proxy.

The SQL reads facts and stable decision diagnostics. Cosines use the production
core and vectors use the sole claim-index adapter. No model, cache, send or
database write is involved. Similarity is a proxy, not a same-fact judgment.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from tracefold.news.claim_recall import (
    CALIBRATION,
    RECALL_POLICY,
    RECEIPT_WINDOW_MS,
    Probe,
    dense_scores,
    embed_text,
    text_sha,
)
from tracefold.news.notifications.novelty import ClaimLink, current_links
from tracefold.news.storage.claim_index import ClaimIndexStorage
from tracefold.news.updates.contracts import Claim


def proxy_pairs(
    receipts: list[dict[str, Any]], candidates: dict[str, Any], links: tuple[ClaimLink, ...]
) -> dict[str, Any]:
    adjacency: dict[str, set[str]] = {}
    for link in current_links(links):
        if link.relation in {"equivalent", "adds_information", "real_world_change", "corrects", "conflicts"}:
            adjacency.setdefault(link.current_ref, set()).add(link.previous_ref)
            adjacency.setdefault(link.previous_ref, set()).add(link.current_ref)
    prior: list[dict[str, Any]] = []
    count = comparisons = missing = 0
    examples = []
    for receipt in sorted(receipts, key=lambda r: (r["settled_at_ms"], r["intent_id"])):
        eligible = [p for p in prior if p["settled_at_ms"] < receipt["settled_at_ms"]]
        frozen = [Claim.model_validate(c) for c in receipt["sent_claims"] or ()]
        for claim in frozen:
            row = candidates[f"{claim.ref}:{text_sha(claim)}"]
            scores = dense_scores(
                Probe(embed_text(claim), row.vector, row.embedder),
                [replace(p["candidate"], key=p["key"]) for p in eligible],
            )
            comparisons += len(scores)
            missing += len(eligible) - len(scores)
            reached = set(adjacency.get(claim.ref, ()))
            reached.update(ref for neighbor in tuple(reached) for ref in adjacency.get(neighbor, ()))
            anchors = {
                d["reader"]["earlier"]["intent_id"]
                for d in (receipt.get("plan") or {}).get("claim_decisions", ())
                if d.get("claim_ref") == claim.ref and (d.get("reader") or {}).get("earlier")
            }
            for old in eligible:
                if (
                    scores.get(old["key"], -1) >= CALIBRATION.receipt.dense_floor
                    and old["claim_ref"] not in reached
                    and old["claim_ref"] != claim.ref
                    and old["intent_id"] not in anchors
                ):
                    count += 1
                    if len(examples) < 20:
                        examples.append(
                            {
                                "later_intent": receipt["intent_id"],
                                "later_claim": claim.ref,
                                "earlier_intent": old["intent_id"],
                                "earlier_claim": old["claim_ref"],
                                "cosine": scores[old["key"]],
                            }
                        )
        prior.extend(
            {
                "key": f"{receipt['intent_id']}:{c.ref}:{text_sha(c)}",
                "intent_id": receipt["intent_id"],
                "claim_ref": c.ref,
                "candidate": candidates[f"{c.ref}:{text_sha(c)}"],
                "settled_at_ms": receipt["settled_at_ms"],
            }
            for c in frozen
        )
    return {"pairs": count, "vector_comparisons": comparisons, "unknown_vector_pairs": missing, "examples": examples}


def read_facts(
    conn: Any, *, as_of_ms: int
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any], tuple[ClaimLink, ...]]:
    statistics = dict(
        conn.execute(
            Path(__file__).with_suffix(".sql").read_text(),
            {"from_ms": as_of_ms - 86400_000, "as_of_ms": as_of_ms},
        ).fetchone()["statistics"]
    )
    receipts = list(
        conn.execute(
            "SELECT intent_id,settled_at_ms,sent_claims,plan FROM news_notifications "
            "WHERE kind='update' AND state='sent' AND settled_at_ms >= %s AND settled_at_ms < %s",
            (as_of_ms - RECEIPT_WINDOW_MS, as_of_ms),
        ).fetchall()
    )
    claims = {
        f"{c.ref}:{text_sha(c)}": c
        for r in receipts
        for value in r["sent_claims"] or ()
        for c in (Claim.model_validate(value),)
    }
    candidates = {c.key: c for c in ClaimIndexStorage(conn).claim_candidates(claims)}
    refs = sorted({c.ref for c in claims.values()})
    links = tuple(
        ClaimLink(
            current_ref=r["current_ref"],
            previous_ref=r["previous_ref"],
            relation=r["relation"],
            asserted_at_ms=r["asserted_at_ms"],
        )
        for r in conn.execute(
            """SELECT c->>'current_ref' AS current_ref,c->>'previous_ref' AS previous_ref,
                      c->>'relation' AS relation,a.adopted_at_ms AS asserted_at_ms
                 FROM news_analyses a CROSS JOIN LATERAL jsonb_array_elements(a.document->'changes') c
                WHERE a.adopted_at_ms < %s AND c->>'previous_ref' IS NOT NULL
                  AND c->>'previous_ref'<>c->>'current_ref'
                  AND c->>'relation' IN ('equivalent','adds_information','real_world_change','corrects','conflicts')
                  AND (c->>'current_ref'=ANY(%s::text[]) OR c->>'previous_ref'=ANY(%s::text[]))""",
            (as_of_ms, refs, refs),
        ).fetchall()
    )
    statistics["sent_receipts_missing_projection"] = sum(r["sent_claims"] is None for r in receipts)
    return statistics, receipts, candidates, links


def measure(
    facts: tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any], tuple[ClaimLink, ...]], *, as_of_ms: int
) -> dict[str, Any]:
    statistics, receipts, candidates, links = facts
    statistics["useful_relation_rate"] = (
        statistics["useful_relation_outputs"] / statistics["relation_pairs"] if statistics["relation_pairs"] else None
    )
    for consumer in ("prior", "receipt"):
        statistics[f"{consumer}_degraded_fraction"] = (
            statistics[f"{consumer}_degraded"] / statistics[f"{consumer}_calls"]
            if statistics[f"{consumer}_calls"]
            else None
        )
    return {
        "as_of_ms": as_of_ms,
        "policy": RECALL_POLICY,
        "statistics": statistics,
        "missed_recall_proxy": proxy_pairs(receipts, candidates, links),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of-ms", type=int, default=int(time.time() * 1000))
    args = parser.parse_args()
    # The DSN is never an argument or output; the connection is read-only from startup.
    with (
        psycopg.connect(
            os.environ["TRACEFOLD_READONLY_DSN"],
            options="-c default_transaction_read_only=on -c statement_timeout=15000",
            row_factory=dict_row,
        ) as conn,
        conn.transaction(),
    ):
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        facts = read_facts(conn, as_of_ms=args.as_of_ms)
    report = measure(facts, as_of_ms=args.as_of_ms)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
