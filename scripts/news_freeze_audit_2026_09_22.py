"""Freeze the 2026-09-22 independent audit (1,491 labels) as `news_review_v7` judgments (#675 §4).

One-shot. The audit was 14 independent Opus reviewers labelling every model-origin verdict in the
2026-09-21 07:10 -> 09-22 07:10 UTC window against `RUBRIC.md`: keep / demote / borderline on delivered
cards, ok_drop / missed_valuable / borderline on the rest. Those labels are the only end-to-end reading
this corpus has of "did the reader want this", and they live in a scratchpad JSON file until something
writes them where the desk, the funnel ratios and the frozen datasets can see them.

What it writes, and what it deliberately does not.

- `should_push` only, with `dimensions={"timeliness": "not_applicable"}`. That is the minimal honest
  shape: the DB CHECK refuses an empty `dimensions`, `must_push`/`should_push` require a `timeliness`
  entry, and `not_applicable` produces no component target at all (`learning/supervision.py`). The
  reviewers read the headline, the card and the decision -- they did not read the frozen evidence
  snapshot, so a `factual_fidelity`, `asset_grounding` or `why_*` label from this batch would be a
  fabricated citation. These cases enter the corpus with `targets=()`; their value is an end-to-end
  baseline and a seed pool, not component supervision.
- The auditor identity goes in `reviewer` (a principal subject, <= 64 chars). `label_source` is a closed
  enum that only governs the taxonomy axis, so it stays untouched.
- `category` and the reviewer's sentence go in `note` as `category=<category>; <reason>`. 24 category
  values do not become an enum for one batch.
- `gate_correct`, `throttle_correct` and `price_basis` are not written anywhere. The first two belong to
  the replay fixture that already carries them; `price_basis` becomes `fact_kind` under PR-2.

Verdict mapping (#675 §4):

    keep            -> should_push
    demote          -> should_hold
    ok_drop         -> should_hold, or must_hold when category is marketing or schedule
    borderline      -> uncertain
    missed_valuable -> must_push for the six the issue's first comment confirmed after cross-checking
                       the full delivery ledger (4 real misses + 2 delayed), uncertain for the other 11,
                       which that cross-check showed were the same fact already delivered.

Skips, printed individually: an Event whose review task the desk cannot open (its evidence snapshot is
gone) and an Event whose evidence is not release-eligible. Neither is guessed at.

Safety. `--dry-run` is the default and writes nothing. A real write needs `--execute` *and*
`--confirm independent_audit_2026-09-22`, because it appends 1,491 judgments and 1,491 acceptances to an
append-only plane and there is no undo.

    uv run python scripts/news_freeze_audit_2026_09_22.py --reviews /path/to/merged_reviews.json
    uv run python scripts/news_freeze_audit_2026_09_22.py --reviews ... \\
        --execute --confirm independent_audit_2026-09-22
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

REVIEWER = "independent_audit_2026-09-22"
DIMENSIONS = {"timeliness": "not_applicable"}
MUST_HOLD_CATEGORIES = frozenset({"marketing", "schedule"})

# The six `missed_valuable` labels that survived the cross-check against the full delivery ledger and the
# `restates` pointers (#675, first comment §3): four the reader never got, two delivered hours late. The
# other eleven named a fact an earlier card had already carried, so they are frozen as `uncertain`.
CONFIRMED_MISSES: Mapping[str, str] = {
    "6e56c8f2cbf86fa6114e42e63f39ec08849263c60ddaf253e8cd92c2dd1e89c5": "4个休眠钱包向Bitfinex存入48,047枚ETH",
    "e69bd2fe1ab1803c0cf7dde1381f5f974623b0b0b6dc4386643ece0341ada467": "美国战略石油储备原油库存降至1982年以来最低",
    "86c87d3803bccdf2378ee4e701c155139d30196a0c9b9cd837f9e5f18b7e34b8": "美联储古尔斯比：需更激进更早行动",
    "3726cd387a1d52e4e8bf735c92cc408e643ad3d265de13fcfc289e53c2b6b337": "卡塔尔能源称霍尔木兹危机或致LNG扩建项目延迟",
    # delayed: throttled at 12:12, delivered 18:09
    "a1f09fb973c1f569a7bbd77910fb29fb452d307521074e7f4bf339ae7d3eabf8": "贝森特称伊朗航空9/23停运",
    # delayed: throttled at 15:00, another source of the same fact went at 15:51
    "563766e52c1246bd12049dd8ac28bda5dbd1c148c48839fdc5276a89c12793f2": "沙特波斯湾装载量创6月来新高",
}


def should_push_for(review: Mapping[str, Any]) -> str:
    """The audit verdict as a `news_review_v7` push label."""

    verdict = str(review.get("verdict") or "")
    category = str(review.get("category") or "")
    if verdict == "keep":
        return "should_push"
    if verdict == "demote":
        return "should_hold"
    if verdict == "ok_drop":
        return "must_hold" if category in MUST_HOLD_CATEGORIES else "should_hold"
    if verdict == "missed_valuable":
        return "must_push" if str(review.get("event_id") or "") in CONFIRMED_MISSES else "uncertain"
    if verdict == "borderline":
        return "uncertain"
    raise ValueError(f"news_freeze_audit_unknown_verdict:{verdict}")


def submission_for(review: Mapping[str, Any]) -> dict[str, Any]:
    """The exact `EventRubricSubmission` body this label becomes."""

    note = f"category={review.get('category') or 'other'!s}; {str(review.get('reason') or '').strip()}"
    return {
        "kind": "event_rubric",
        "should_push": should_push_for(review),
        "dimensions": dict(DIMENSIONS),
        "note": note[:2_000],
    }


def _connect(dsn: str) -> Any:
    import psycopg
    from psycopg.rows import dict_row

    return psycopg.connect(dsn, row_factory=dict_row)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn", default="postgresql://tracefold@127.0.0.1:56533/tracefold")
    parser.add_argument("--reviews", type=Path, required=True, help="merged_reviews.json from the 09-22 audit")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="actually submit; without it the script only reports what it would write",
    )
    parser.add_argument(
        "--confirm",
        default="",
        help=f"must be exactly {REVIEWER!r} alongside --execute; the write is append-only and cannot be undone",
    )
    args = parser.parse_args()
    if args.execute and str(args.confirm) != REVIEWER:
        raise SystemExit(f"refusing to write: --execute requires --confirm {REVIEWER}")

    from tracefold.news.review.desk import DeskQuery, EventRubricSubmission, Principal, ReviewDesk, TaskRef

    reviews = json.loads(args.reviews.read_text(encoding="utf-8"))["reviews"]
    principal = Principal(subject=REVIEWER)
    planned: list[tuple[str, str, str, dict[str, Any]]] = []
    labels: dict[str, int] = {}
    skipped: dict[str, list[str]] = {"task_unavailable": [], "not_release_eligible": [], "rubric_rejected": []}

    with _connect(args.dsn) as conn:
        desk = ReviewDesk(conn)
        for event_id, review in sorted(reviews.items()):
            payload = submission_for({**review, "event_id": event_id})
            queue = desk.open(
                # The `event` branch resolves the Event's newest evidence version directly; it applies no
                # look-back window, so an Event older than the queue's default 24 h still resolves.
                DeskQuery(view="queue", mode="event", event=str(event_id), status="all"),
                principal=principal,
            )
            rows = list(queue.get("tasks") or ())
            if not rows:
                skipped["task_unavailable"].append(str(event_id))
                continue
            task = dict(rows[0])
            if not task.get("evidence_ready"):
                skipped["not_release_eligible"].append(str(event_id))
                continue
            try:
                EventRubricSubmission.model_validate(payload)
            except Exception as exc:
                skipped["rubric_rejected"].append(f"{event_id}: {type(exc).__name__}")
                continue
            planned.append((str(event_id), str(task["task_id"]), str(task["task_version"]), payload))
            labels[str(payload["should_push"])] = labels.get(str(payload["should_push"]), 0) + 1

        print(f"labels read: {len(reviews)}")
        print(f"resolvable and release-eligible: {len(planned)}")
        for label, count in sorted(labels.items()):
            print(f"  {label}: {count}")
        for reason, items in skipped.items():
            print(f"skipped {reason}: {len(items)}")
            for item in items:
                print(f"  {item}")
        if not args.execute:
            print("dry run: nothing written")
            return

        from tracefold.platform.postgres.client import transaction

        submitted, failures = 0, []
        for event_id, task_id, task_version, payload in planned:
            # One transaction per label, like `accept-drafts`: one rubric the desk refuses must not roll
            # back the batch. The idempotency key is stable, so an interrupted run resumes without doubling.
            try:
                with transaction(conn):
                    desk.submit(
                        TaskRef(task_id=task_id, task_version=task_version),
                        EventRubricSubmission.model_validate(payload),
                        principal=principal,
                        idempotency_key=f"{REVIEWER}:{event_id}"[:128],
                    )
                submitted += 1
            except Exception as exc:
                failures.append(f"{event_id}: {type(exc).__name__}: {str(exc)[:160]}")
        print(f"submitted: {submitted} / {len(planned)}")
        for failure in failures[:40]:
            print(f"  failed {failure}")


if __name__ == "__main__":
    main()
