from __future__ import annotations

import json
import uuid
from argparse import Namespace
from typing import Any

from tracefold.platform.config.loader import load_settings


def _handle_review(args: Namespace) -> tuple[int, dict[str, Any]]:
    from tracefold.app.repository_session import postgres_connection
    from tracefold.news.review.desk import (
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
                cohort=args.cohort,
                stratum=args.stratum,
                task=args.task,
                event=args.event,
                status=args.status,
                hours=int(args.hours),
                limit=min(100, int(args.limit)),
                cursor=args.cursor,
            )
            with postgres_connection(settings) as conn:
                data = ReviewDesk(conn).open(query, principal=principal)
            return 0, {"ok": True, "data": data}
        if action == "evidence":
            task = TaskRef(task_id=str(args.task), task_version=str(args.version))
            with postgres_connection(settings) as conn:
                data = ReviewDesk(conn).evidence(task, principal=principal, source_only=bool(args.source_only))
            return 0, {"ok": True, "data": data}

        payload = _read_json_or_yaml(str(args.file))
        key = str(args.idempotency_key or uuid.uuid4())
        if action == "submit":
            reviewer = str(args.reviewer or "").strip()
            if not reviewer:
                raise ValueError("news_review_submit_reviewer_required")
            principal = Principal(subject=reviewer)
        # The HTTP pool is connection-level read-only. This short-lived CLI connection uses the shared
        # login's ordinary transaction mode; since #256 it is the only ReviewDesk writer.
        with postgres_connection(settings) as conn, transaction(conn):
            desk = ReviewDesk(conn)
            submission: ReviewSubmission
            if action == "external-miss":
                submission = ExternalMissSubmission.model_validate(payload)
                data = desk.submit(None, submission, principal=principal, idempotency_key=key)
            else:
                submission = EventRubricSubmission.model_validate(payload)
                task = TaskRef(task_id=str(args.task), task_version=str(args.version))
                data = desk.submit(task, submission, principal=principal, idempotency_key=key)
        return 0, {"ok": True, "data": data}
    except (ValueError, PermissionError) as exc:
        return 2, {"ok": False, "error": str(exc)}


def _read_json_or_yaml(path: str) -> dict[str, Any]:
    """JSON first, YAML second: a hand-written review file is allowed to be YAML."""

    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    try:
        document = json.loads(text)
    except ValueError:
        import yaml

        document = yaml.safe_load(text)
    if not isinstance(document, dict):
        raise ValueError(f"news_document_not_a_mapping:{path}")
    return document
