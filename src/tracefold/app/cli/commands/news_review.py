from __future__ import annotations

import uuid
from argparse import Namespace
from typing import Any

from tracefold.platform.config.loader import load_settings

from .news_learning_documents import _read_json_or_yaml


def _handle_review(args: Namespace) -> tuple[int, dict[str, Any]]:
    from tracefold.app.repository_session import postgres_connection
    from tracefold.news.review.desk import (
        BlindPairwiseSubmission,
        DeskQuery,
        EventRubricSubmission,
        ExternalMissSubmission,
        Principal,
        ReviewDesk,
        ReviewSubmission,
        TaskRef,
    )
    from tracefold.platform.postgres.client import transaction

    settings = load_settings(require_ws_token=False)
    principal = Principal(subject="operator")
    action = str(args.review_command)
    try:
        if action == "queue":
            query = DeskQuery(
                view=args.view,
                mode=args.mode,
                cohort=args.cohort,
                stratum=args.stratum,
                proposal=args.proposal,
                task=args.task,
                event=args.event,
                status=args.status,
                hours=int(args.hours),
                limit=min(100, int(args.limit)),
                cursor=args.cursor,
            )
            with postgres_connection(settings, role="serve") as conn:
                data = ReviewDesk(conn).open(query, principal=principal)
            return 0, {"ok": True, "data": data}
        if action == "evidence":
            task = TaskRef(task_id=str(args.task), task_version=str(args.version))
            with postgres_connection(settings, role="serve") as conn:
                data = ReviewDesk(conn).evidence(task, principal=principal)
            return 0, {"ok": True, "data": data}

        if action == "accept-drafts":
            return _handle_review_accept_drafts(args, settings, principal)

        payload = _read_json_or_yaml(str(args.file))
        kind = str(payload.get("kind") or "")
        key = str(args.idempotency_key or uuid.uuid4())
        # `postgres_connection` accepts serve|workers|migrate; `review` was never one of them, so this path
        # has raised `AttributeError: 'PostgresConfig' object has no attribute 'review_dsn'` since #112 and
        # every existing review reached the table through the HTTP API instead. `tracefold_serve` is the role
        # that actually holds INSERT on `news_reviews`, and it defaults to read-only — the HTTP ReviewDesk
        # route opts one transaction into those two grants the same way (`serve_runtime.review_transaction`).
        with postgres_connection(settings, role="serve") as conn, transaction(conn):
            conn.execute("SET TRANSACTION READ WRITE")
            desk = ReviewDesk(conn)
            submission: ReviewSubmission
            if action == "external-miss":
                submission = ExternalMissSubmission.model_validate(payload)
                data = desk.submit(None, submission, principal=principal, idempotency_key=key)
            else:
                submission = (
                    EventRubricSubmission.model_validate(payload)
                    if kind == "event_rubric"
                    else BlindPairwiseSubmission.model_validate(payload)
                )
                task = TaskRef(task_id=str(args.task), task_version=str(args.version))
                data = desk.submit(task, submission, principal=principal, idempotency_key=key)
        return 0, {"ok": True, "data": data}
    except (ValueError, PermissionError) as exc:
        return 2, {"ok": False, "error": str(exc)}


def _handle_review_accept_drafts(args: Namespace, settings: Any, principal: Any) -> tuple[int, dict[str, Any]]:
    """Submit model drafts the operator has read, through the ordinary submit path.

    This is deliberately not a shortcut around review. `ReviewDesk.submit` stays the only writer, every row is
    still authored by the operator running this command, and the rubric's own validators still decide what is
    acceptable. What it removes is retyping: the drafter turned "compose 300 judgments" into "read 300 and
    say which ones are right", and this turns the second half into one command.

    Measured against 25 Events a human had already judged, the drafter agrees 70-88% on the dimensions it is
    allowed to emit. That is useful and it is not good enough to accept unread — hence `--dry-run`,
    `--min-confidence` and the explicit include/exclude lists, and hence the receipt naming exactly what went in.
    """

    from tracefold.app.repository_session import postgres_connection
    from tracefold.news.review.desk import EventRubricSubmission, Principal, ReviewDesk, TaskRef
    from tracefold.news.review.drafter import DRAFT_SCHEMA, ReviewDraft, submission_payload
    from tracefold.platform.postgres.client import transaction

    # A distinguishable author, on purpose. Measured against 25 Events a human had already judged, the drafter
    # agrees 70-88% on the dimensions it may emit, so a bulk accept carries roughly 15-20% wrong dimension
    # labels. That is a reasonable trade for a corpus that is otherwise too small to measure anything — but
    # only while it stays visible: one `reviewer <> 'model_drafted_operator'` filter takes every one of these
    # back out, which is impossible if they are indistinguishable from a human's judgment.
    principal = Principal(subject=str(args.reviewer))

    batch = _read_json_or_yaml(str(args.file))
    if str(batch.get("schema_id") or "") != DRAFT_SCHEMA:
        raise ValueError("news_review_accept_drafts_schema_invalid")
    minimum = float(args.min_confidence)
    only = tuple(part.strip() for part in str(args.only).split(",") if part.strip())
    exclude = tuple(part.strip() for part in str(args.exclude).split(",") if part.strip())

    planned: list[tuple[str, str, dict[str, Any], float]] = []
    skipped: dict[str, int] = {}

    def skip(reason: str) -> None:
        skipped[reason] = skipped.get(reason, 0) + 1

    for entry in batch.get("drafts") or ():
        task_id, event_id = str(entry.get("task_id") or ""), str(entry.get("event_id") or "")
        if entry.get("error"):
            skip("drafting_failed")
            continue
        if only and not any(task_id.startswith(p) or event_id.startswith(p) for p in only):
            skip("not_in_only")
            continue
        if exclude and any(task_id.startswith(p) or event_id.startswith(p) for p in exclude):
            skip("excluded")
            continue
        draft = ReviewDraft.model_validate(entry.get("draft") or {})
        if draft.confidence < minimum:
            skip("below_min_confidence")
            continue
        try:
            payload = submission_payload(draft)
            EventRubricSubmission.model_validate(payload)
        except Exception:
            # The rubric refused it. Better skipped and reported than reshaped into something acceptable.
            skip("rubric_rejected")
            continue
        planned.append((task_id, str(entry.get("task_version") or ""), payload, draft.confidence))

    if bool(args.dry_run):
        return 0, {
            "ok": True,
            "data": {
                "dry_run": True,
                "would_submit": len(planned),
                "skipped": skipped,
                "batch_sha256": batch.get("batch_sha256"),
                "sample": [
                    {"task_id": task_id, "confidence": confidence, "dimensions": payload["dimensions"]}
                    for task_id, _version, payload, confidence in planned[:5]
                ],
            },
        }

    submitted, failures = 0, []
    with postgres_connection(settings, role="serve") as conn:
        for task_id, task_version, payload, _confidence in planned:
            # One transaction per draft: a rubric one Event disagrees with must not roll back the rest.
            try:
                with transaction(conn):
                    conn.execute("SET TRANSACTION READ WRITE")
                    ReviewDesk(conn).submit(
                        TaskRef(task_id=task_id, task_version=task_version),
                        EventRubricSubmission.model_validate(payload),
                        principal=principal,
                        idempotency_key=_sha_idempotency(batch.get("batch_sha256"), task_id),
                    )
                submitted += 1
            except Exception as exc:
                failures.append({"task_id": task_id, "error": f"{type(exc).__name__}: {str(exc)[:160]}"})
    return 0, {
        "ok": True,
        "data": {
            "submitted": submitted,
            "planned": len(planned),
            "skipped": skipped,
            "failed": failures[:20],
            "failed_n": len(failures),
            "batch_sha256": batch.get("batch_sha256"),
            "reviewer": getattr(principal, "subject", None),
        },
    }


def _sha_idempotency(batch_sha: Any, task_id: str) -> str:
    """Stable per (batch, task), so re-running an interrupted accept does not double-write."""

    from tracefold.news.artifact_identity import canonical_sha

    return canonical_sha({"batch": str(batch_sha or ""), "task_id": task_id})
