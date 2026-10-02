"""Read-only fit/replay of shared claim rank against the #791 blinded pair corpus.

The source NPZ contains numeric arrays only (allow_pickle=False). PostgreSQL
writes are limited to connection-local TEMP tables. No persistent facts or
judgment cache are written, no model is called and no send is possible.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import defaultdict
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import numpy as np
import psycopg

from tracefold.news.claim_recall import CALIBRATION, Candidate, Cuts, Probe, prepare_rank, rank, vector_bytes


def read_rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def load_corpus(root: Path, *, dsn: str) -> tuple[list[dict[str, Any]], str]:
    data = root / "data"
    sample = json.loads((data / "sample.json").read_text())
    units = json.loads((data / "units.json").read_text())
    claims: dict[str, Any] = {}
    for row in read_rows(data / "claims_raw.jsonl"):
        ref = row["claim"]["ref"]
        if ref not in claims or row["adopted"] < claims[ref]["adopted"]:
            claims[ref] = row
    receipts = read_rows(data / "receipts.jsonl")
    for receipt in receipts:
        for c in receipt["sent_claims"] or ():
            claims.setdefault(c["ref"], {"claim": c, "event_id": receipt["event_id"], "adopted": receipt["settled"]})
    labels: dict[str, dict[str, str]] = defaultdict(dict)
    order = {"SF": 3, "SD": 2, "TO": 1, "U": 0}
    for line in (root / "labels" / "labels.txt").read_text().splitlines():
        fields = line.split()
        if not fields:
            continue
        query = fields[0]
        label, ids = (fields[1], fields[2:]) if fields[1] in order else (fields[2], fields[1:2])
        for key in ids:
            if order[label] >= order.get(labels[query].get(key, "U"), 0):
                labels[query][key] = label
    index = json.loads((data / "emb_mini_index.json").read_text())
    arrays = np.load(data / "emb_mini.npz", allow_pickle=False)
    vectors = {r: vector_bytes(v, CALIBRATION.embedder) for r, v in zip(index["claims"], arrays["claims"], strict=True)}
    # FTS is the production PostgreSQL implementation, never a Python twin.
    with psycopg.connect(dsn) as conn:
        conn.execute("CREATE TEMP TABLE evaluation_claims(ref text PRIMARY KEY,text text,lexical tsvector)")
        with conn.cursor().copy("COPY evaluation_claims(ref,text) FROM STDIN") as copy:
            for ref, row in claims.items():
                copy.write_row((ref, " ".join(row["claim"]["statement"].split())))
        conn.execute("UPDATE evaluation_claims SET lexical=to_tsvector('english',text)")
        queries = []
        for number, row in enumerate(sample, 1):
            qid = f"Q{number:03d}"
            ref, timestamp = row["ref"], int(row["adopted"])
            if ref not in vectors:
                raise ValueError(f"query_vector_missing:{qid}")
            text = " ".join(claims[ref]["claim"]["statement"].split())
            query = conn.execute(
                """SELECT COALESCE(string_agg(quote_literal(term),' | '),'')::tsquery
                     FROM unnest(tsvector_to_array(to_tsvector('english',%s))) term""",
                (text,),
            ).fetchone()[0]
            lexical = dict(
                conn.execute(
                    "SELECT ref,ts_rank_cd(lexical,%s::tsquery,32) FROM evaluation_claims WHERE lexical @@ %s::tsquery",
                    (query, query),
                ).fetchall()
            )
            by_ref = {r: u["id"] for u in units[qid]["units"] if u["kind"] == "C" for r in u["refs"]}
            # Blind units group normalized statements across Event-local refs.
            unit_by_text = {
                re.sub(r"\W+", " ", claims[r]["claim"]["statement"].lower()).strip(): unit
                for r, unit in by_ref.items()
                if r in claims
            }
            by_ref.update(
                {
                    r: unit_by_text[text]
                    for r, c in claims.items()
                    for text in (re.sub(r"\W+", " ", c["claim"]["statement"].lower()).strip(),)
                    if text in unit_by_text
                }
            )
            sent = {
                c["ref"]
                for r in receipts
                if timestamp - 48 * 3600_000 <= r["settled"] < timestamp
                for c in r["sent_claims"] or ()
            }
            prior = tuple(
                Candidate(
                    r,
                    vectors.get(r),
                    CALIBRATION.embedder.key if r in vectors else None,
                    lexical=float(lexical.get(r, 0)),
                    sent=r in sent,
                )
                for r, c in claims.items()
                if c["event_id"] != row["event_id"] and timestamp - 7 * 86400_000 <= c["adopted"] < timestamp
            )
            receipt_candidates = tuple(
                Candidate(
                    f"{r['intent_id']}:{c['ref']}",
                    vectors.get(c["ref"]),
                    CALIBRATION.embedder.key if c["ref"] in vectors else None,
                    lexical=float(lexical.get(c["ref"], 0)),
                    group=r["intent_id"],
                )
                for r in receipts
                if timestamp - 48 * 3600_000 <= r["settled"] < timestamp
                for c in r["sent_claims"] or ()
            )
            receipt_labels = {u["intent"]: labels[qid].get(u["id"]) for u in units[qid]["units"] if u["kind"] == "R"}
            for r in receipts:
                for c in r["sent_claims"] or ():
                    unit_label = labels[qid].get(by_ref.get(c["ref"], ""))
                    previous = receipt_labels.get(r["intent_id"])
                    if unit_label is not None and (previous is None or order[unit_label] > order[previous]):
                        receipt_labels[r["intent_id"]] = unit_label
            queries.append(
                {
                    "id": qid,
                    "probe": Probe(text, vectors[ref], CALIBRATION.embedder.key),
                    "prior": prior,
                    "receipt": receipt_candidates,
                    "prior_gold": {key for key, lab in labels[qid].items() if lab == "SF" and key.startswith("c")},
                    "receipt_gold": {
                        r["intent_id"]
                        for r in receipts
                        if receipt_labels.get(r["intent_id"]) == "SF"
                        and timestamp - 48 * 3600_000 <= r["settled"] < timestamp
                    },
                    "by_ref": by_ref,
                    "labels": labels[qid],
                    "receipt_labels": receipt_labels,
                    "stratum": row.get("stratum"),
                    "src_script": row.get("script"),
                    "baseline_prior": len(row["p1"]),
                    "baseline_receipt": row.get("p2", []),
                }
            )
    digest = hashlib.sha256()
    for path in (
        data / "sample.json",
        data / "units.json",
        data / "claims_raw.jsonl",
        data / "receipts.jsonl",
        root / "labels" / "labels.txt",
        data / "emb_mini_index.json",
        data / "emb_mini.npz",
    ):
        digest.update(path.name.encode())
        with path.open("rb") as stream:
            while block := stream.read(1024 * 1024):
                digest.update(block)
    return queries, digest.hexdigest()


def metrics(
    queries: list[dict[str, Any]], calibration: Any, consumer: str, *, degraded: bool = False
) -> dict[str, Any]:
    gold_queries = successes = selected = unrelated = labeled = comparisons = baseline = 0
    strata: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for q in queries:
        probe = Probe(q["probe"].text) if degraded else q["probe"]
        rows = q[consumer]
        result = rank(prepare_rank(probe, rows, consumer, calibration=calibration), rows)
        keys = [h.key for h in result.hits]
        got = {q["by_ref"].get(k) for k in keys} if consumer == "prior" else set(keys)
        gold = q[f"{consumer}_gold"]
        if gold:
            gold_queries += 1
            successes += bool(got & gold)
            name = str(q["src_script"])
            strata[name][1] += 1
            strata[name][0] += bool(got & gold)
        selected += len(keys)
        labs = q["labels"] if consumer == "prior" else q["receipt_labels"]
        selected_labels = [labs.get(q["by_ref"].get(k)) if consumer == "prior" else labs.get(k) for k in keys]
        labeled += sum(label is not None for label in selected_labels)
        unrelated += sum(label in {"TO", "U"} for label in selected_labels)
        comparisons += len(keys)
        baseline += q["baseline_prior"]
    return {
        "queries": len(queries),
        "sf_queries": gold_queries,
        "sf_success": successes,
        "sf_success_rate": successes / gold_queries if gold_queries else None,
        "strata": {k: {"success": v[0], "total": v[1]} for k, v in strata.items()},
        "selected": selected,
        "labeled_selected": labeled,
        "unjudged_selected": selected - labeled,
        "unrelated_labeled_fraction": unrelated / labeled if labeled else None,
        "comparison_change": comparisons / baseline - 1 if baseline and consumer == "prior" else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audit-dir", type=Path, required=True)
    parser.add_argument("--postgres-dsn", required=True, help="Isolated evaluation DB; only TEMP tables are written")
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--calibration", type=Path)
    args = parser.parse_args()
    queries, dataset = load_corpus(args.audit_dir, dsn=args.postgres_dsn)
    fitted = replace(CALIBRATION, dataset_sha256=dataset)
    report = {
        "dataset_sha256": dataset,
        "embedder": asdict(fitted.embedder),
        "prior": metrics(queries, fitted, "prior"),
        "receipt": metrics(queries, fitted, "receipt"),
        "degraded_prior": metrics(queries, fitted, "prior", degraded=True),
        "degraded_receipt": metrics(queries, fitted, "receipt", degraded=True),
    }
    grid = []
    for k in (4, 5, 6, 8):
        for floor in (0.4, 0.45, 0.5, 0.55):
            for lexical in (0.05, 0.1, 0.2):
                candidate = replace(fitted, prior=Cuts(k, floor, lexical, lexical, sent_reserved=2))
                score = metrics(queries, candidate, "prior")
                grid.append({"k": k, "dense_floor": floor, "lexical_floor": lexical, **score})
    report["prior_grid"] = grid
    if args.calibration:
        eligible = [
            r
            for r in grid
            if r["sf_success_rate"] >= 0.85
            and all(
                r["strata"].get(s, {"success": 0, "total": 1})["success"]
                / r["strata"].get(s, {"success": 0, "total": 1})["total"]
                >= 0.9
                for s in ("zh", "ru")
            )
        ]
        if not eligible:
            raise ValueError("recall_prior_acceptance_failed")
        best = min(eligible, key=lambda r: (r["selected"], -r["sf_success_rate"], r["k"], r["dense_floor"]))
        fitted = replace(
            fitted,
            prior=Cuts(best["k"], best["dense_floor"], best["lexical_floor"], best["lexical_floor"], sent_reserved=2),
        )
        report["prior"] = metrics(queries, fitted, "prior")
        report["degraded_prior"] = metrics(queries, fitted, "prior", degraded=True)
        report["selected_cuts"] = {"prior": asdict(fitted.prior), "receipt": asdict(fitted.receipt)}
        config = {
            "version": "claim_recall_v1",
            "embedder": asdict(fitted.embedder),
            "prior": asdict(
                Cuts(best["k"], best["dense_floor"], best["lexical_floor"], best["lexical_floor"], sent_reserved=2)
            ),
            "receipt": asdict(fitted.receipt),
            "route_n": fitted.route_n,
            "rrf_k": fitted.rrf_k,
            "dataset_sha256": dataset,
        }
        args.calibration.write_text(json.dumps(config, indent=2) + "\n")
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({key: value for key, value in report.items() if key != "prior_grid"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
