"""ReviewDesk for current News notification decisions and external miss feedback."""

from __future__ import annotations

import base64
import binascii
import json
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..artifact_identity import canonical_json, canonical_sha

REVIEW_QUEUE_MAX = 100
REVIEW_BODY_TEXT_MAX = 20_000
DECISION_REVIEW_VERSION: Final = "news_reader_review_v1"
DECISION_READER_CONTRACT_VERSION: Final = "reader_decision_v2"


class Principal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    subject: str = Field(min_length=1, max_length=64)
    can_review: bool = True


class DeskQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    view: Literal["queue", "coverage"] = "queue"
    cohort: str = Field(default="", max_length=160)
    stratum: str = Field(default="", max_length=64)
    task: str = Field(default="", max_length=300)
    event: str = Field(default="", max_length=128)
    status: str = Field(default="pending", max_length=32)
    hours: int = Field(default=24, ge=1, le=720)
    limit: int = Field(default=30, ge=1, le=REVIEW_QUEUE_MAX)
    cursor: str = Field(default="", max_length=300)


class TaskRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str = Field(min_length=1, max_length=300)
    task_version: str = Field(pattern=r"^[0-9a-f]{64}$")


class DecisionFeedbackSubmission(BaseModel):
    """A reviewer labels the usefulness of one claim at the frozen decision input."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["decision_feedback"] = "decision_feedback"
    should_push: Literal["should_push", "should_hold", "uncertain"]
    note: str = Field(default="", max_length=2_000)


class ExternalMissSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["external_miss"] = "external_miss"
    source_url: str = Field(min_length=1, max_length=2_000)
    title: str = Field(min_length=1, max_length=1_000)
    body: str = Field(default="", max_length=REVIEW_BODY_TEXT_MAX)
    occurred_at_ms: int = Field(ge=0)
    feedback: DecisionFeedbackSubmission


ReviewSubmission = ExternalMissSubmission | DecisionFeedbackSubmission


@dataclass(frozen=True, slots=True)
class ReviewReadStatement:
    """One bounded ReviewDesk read shared by serving and query audit."""

    name: str
    sql: str
    params: tuple[Any, ...]


def _decision_task_ref(row: Mapping[str, Any]) -> TaskRef:
    task_id = f"dec.{row['decision_ref']}.{row['claim_ref']}"
    return TaskRef(
        task_id=task_id,
        task_version=_sha(
            {
                "contract": DECISION_REVIEW_VERSION,
                "decision_ref": row["decision_ref"],
                "claim_ref": row["claim_ref"],
                "input_snapshot": row["input_snapshot"],
                "claim_decision": row["claim_decision"],
            }
        ),
    )


def _parse_decision_task_id(value: str) -> tuple[str, str]:
    parts = value.split(".")
    if (
        len(parts) != 3
        or parts[0] != "dec"
        or not parts[1].startswith("notification_decision:")
        or not parts[2].startswith("cl:")
    ):
        raise ValueError("news_review_task_id_invalid")
    return parts[1], parts[2]


def _decision_task_public(row: Mapping[str, Any], accepted: Mapping[str, Any] | None) -> dict[str, Any]:
    ref = _decision_task_ref(row)
    plan = dict(row["plan"])
    claim = dict(row["claim"])
    decision = dict(row["claim_decision"])
    stratum = _stratum(plan, decision)
    reader: Mapping[str, Any] = decision.get("reader") or {}
    judgment: Mapping[str, Any] = reader.get("judgment") or {}
    return {
        "task_id": ref.task_id,
        "task_version": ref.task_version,
        "mode": "decision",
        "event_id": row["event_id"],
        "update_ref": row["update_ref"],
        "decision_ref": row["decision_ref"],
        "claim_ref": row["claim_ref"],
        "opened_at_ms": row["created_at_ms"],
        "headline": claim.get("statement") or "",
        "final_decision": decision.get("decision"),
        "reason": decision.get("reason"),
        "origin": row.get("origin"),
        "novelty": reader.get("novelty"),
        "importance": (judgment.get("importance") or {}).get("value"),
        "reader_backend": judgment.get("backend"),
        "reader_receipt": {
            "state": row.get("delivery_state") or row.get("queue_state"),
            "body": row.get("delivery_body"),
            "payload_sha256": row.get("delivery_sha256"),
            "settled_at_ms": row.get("settled_at_ms"),
            "error_code": row.get("delivery_error_code"),
        },
        "selection": {"stratum": stratum, "selection_version": "news_reader_selection_v1"},
        "review_status": "accepted" if accepted is not None else "pending",
        "accepted_review": None if accepted is None else dict(accepted),
    }


# One stratum per claim for sampling reviews: the reader_v2 reasons, and the editor's for editorial_v1 history.
_STRATA: Final[dict[str, str]] = {
    "reader_unavailable": "reader_unavailable",
    "reader_unassessed": "reader_unavailable",
    "reader_feed": "feed_only",
    "reader_key": "key",
    "known_to_reader": "known",
    "attention_unavailable_default_notify": "reader_unavailable",
    "editor_feed_only": "feed_only",
    "editor_key": "key",
}


def _stratum(plan: Mapping[str, Any], decision: Mapping[str, Any]) -> str:
    reason = str(decision.get("reason") or "")
    if reason in _STRATA:
        return _STRATA[reason]
    return "notify" if decision.get("decision") == "notify" else "not_notified"


_DECISION_STRATUM_SQL = """CASE
    WHEN task.claim_decision->>'reason' IN ('reader_unavailable','reader_unassessed',
                                            'attention_unavailable_default_notify') THEN 'reader_unavailable'
    WHEN task.claim_decision->>'reason' IN ('reader_feed','editor_feed_only') THEN 'feed_only'
    WHEN task.claim_decision->>'reason' IN ('reader_key','editor_key') THEN 'key'
    WHEN task.claim_decision->>'reason'='known_to_reader' THEN 'known'
    WHEN task.claim_decision->>'decision'='notify' THEN 'notify'
    ELSE 'not_notified' END"""
_REVIEWED_ORIGINS_SQL = "task.origin IN ('editorial_v1','reader_v2')"


def _decision_queue_statement(
    *,
    lower_ms: int,
    upper_ms: int,
    event: str = "",
    cohort: str = "",
    cursor: tuple[int, str, str] | None = None,
    status: str = "pending",
    stratum: str = "",
    limit: int = REVIEW_QUEUE_MAX,
) -> ReviewReadStatement:
    filters = [_REVIEWED_ORIGINS_SQL, "task.created_at_ms >= %s", "task.created_at_ms < %s"]
    params: list[Any] = [lower_ms, upper_ms]
    if event:
        filters.append("task.event_id=%s")
        params.append(event)
    if cohort:
        filters.append("COALESCE(task.plan->>'reader_identity',task.plan->>'assessment_identity')=%s")
        params.append(cohort)
    if cursor is not None:
        filters.append("(task.created_at_ms,task.decision_ref,task.claim_ref)<(%s,%s,%s)")
        params.extend(cursor)
    eligible = []
    if status == "pending":
        eligible.append("feedback_review_id IS NULL")
    elif status == "accepted":
        eligible.append("feedback_review_id IS NOT NULL")
    if stratum:
        eligible.append("selection_stratum=%s")
        params.append(stratum)
    params.append(limit)
    return ReviewReadStatement(
        name="news_review_decision_queue",
        sql=f"""SELECT * FROM (
            SELECT task.*, feedback.review_id AS feedback_review_id,
                   feedback.reviewer AS feedback_reviewer,
                   feedback.should_push AS feedback_should_push,
                   feedback.note AS feedback_note,
                   feedback.created_at_ms AS feedback_created_at_ms,
                   {_DECISION_STRATUM_SQL} AS selection_stratum
              FROM news_notification_review_tasks_v1 task
              LEFT JOIN LATERAL (
                SELECT review_id,reviewer,should_push,note,created_at_ms
                  FROM news_notification_feedback f
                 WHERE f.decision_ref=task.decision_ref AND f.claim_ref=task.claim_ref
                 ORDER BY created_at_ms DESC,review_id DESC LIMIT 1
              ) feedback ON TRUE
             WHERE {" AND ".join(filters)}
        ) eligible
        {"WHERE " + " AND ".join(eligible) if eligible else ""}
        ORDER BY created_at_ms DESC,decision_ref DESC,claim_ref DESC LIMIT %s""",  # noqa: S608
        params=tuple(params),
    )


def _decision_coverage_statement(*, lower_ms: int, upper_ms: int) -> ReviewReadStatement:
    return ReviewReadStatement(
        name="news_review_decision_coverage",
        sql="""SELECT count(*) AS claims,
                      count(*) FILTER (WHERE task.claim_decision->>'decision'='notify') AS selected,
                      count(*) FILTER (
                        WHERE task.claim_decision->>'reason' IN ('reader_feed','editor_feed_only')) AS feed_only,
                      count(*) FILTER (
                        WHERE task.claim_decision->>'reason' IN ('reader_unavailable','reader_unassessed',
                                                                 'attention_unavailable_default_notify')
                      ) AS reader_unavailable,
                      count(*) FILTER (WHERE task.delivery_state='sent') AS sent,
                      count(*) FILTER (WHERE EXISTS (
                        SELECT 1 FROM news_notification_feedback f
                         WHERE f.decision_ref=task.decision_ref AND f.claim_ref=task.claim_ref
                      )) AS reviewed
                 FROM news_notification_review_tasks_v1 task
                WHERE task.origin IN ('editorial_v1','reader_v2')
                  AND task.created_at_ms >= %s AND task.created_at_ms < %s""",
        params=(lower_ms, upper_ms),
    )


def _decision_evidence_statement(decision_ref: str, claim_ref: str) -> ReviewReadStatement:
    return ReviewReadStatement(
        name="news_review_decision_evidence",
        sql="""SELECT * FROM news_notification_review_tasks_v1
                WHERE decision_ref=%s AND claim_ref=%s AND origin IN ('editorial_v1','reader_v2')""",
        params=(decision_ref, claim_ref),
    )


class ReviewDesk:
    """Current notification decision and external feedback read/write boundary."""

    def __init__(self, conn: Any, *, now_ms: int | None = None) -> None:
        self._conn = conn
        self._now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)

    def open(self, query: DeskQuery, *, principal: Principal) -> dict[str, Any]:
        self._require_principal(principal)
        if query.view == "coverage":
            return self._decision_coverage(query)
        return self._open_decision_queue(query)

    def evidence(self, task: TaskRef, *, principal: Principal, source_only: bool = False) -> dict[str, Any]:
        self._require_principal(principal)
        if not task.task_id.startswith("dec."):
            raise ValueError("news_review_task_kind_unsupported")
        decision_ref, claim_ref = _parse_decision_task_id(task.task_id)
        row = self._decision_row(decision_ref, claim_ref)
        if row is None:
            raise ValueError("news_review_task_not_found")
        if _decision_task_ref(row).task_version != task.task_version:
            raise ValueError("news_review_task_version_conflict")
        snapshot = dict(row["input_snapshot"])
        if source_only:
            payload = {
                "schema": "tracefold.news.review_decision_source_only.v1",
                "task": task.model_dump(mode="json"),
                "claim": row["claim"],
                "evidence": (row.get("update_document") or {}).get("evidence", []),
            }
            return {**payload, "projection_sha256": canonical_sha(payload)}
        accepted = self._decision_feedback(decision_ref, claim_ref)
        return {
            "task": _decision_task_public(row, accepted),
            "evidence": snapshot,
            "agent": {"decision": row["claim_decision"], "assessment": row["plan"]},
            "disclosure": {"outcome_revealed": True, "pairing": "unpaired", "dataset_role": "discovery"},
        }

    def submit(
        self, task: TaskRef | None, submission: ReviewSubmission, *, principal: Principal, idempotency_key: str
    ) -> dict[str, Any]:
        self._require_principal(principal)
        key = _idempotency_key(idempotency_key)
        request_sha = _sha(
            {
                "task": task.model_dump(mode="json") if task is not None else None,
                "submission": submission.model_dump(mode="json"),
            }
        )
        if isinstance(submission, ExternalMissSubmission):
            if task is not None:
                raise ValueError("news_review_external_miss_task_not_allowed")
            return self._submit_external(
                submission, principal=principal, idempotency_key=key, idempotency_request_sha=request_sha
            )
        if task is None:
            raise ValueError("news_review_task_required")
        if not task.task_id.startswith("dec."):
            raise ValueError("news_review_task_kind_unsupported")
        return self._submit_decision(task, submission, principal=principal, key=key, request_sha=request_sha)

    def _decision_row(self, decision_ref: str, claim_ref: str) -> dict[str, Any] | None:
        statement = _decision_evidence_statement(decision_ref, claim_ref)
        row = self._conn.execute(statement.sql, statement.params).fetchone()
        return None if row is None else dict(row)

    def _decision_feedback(self, decision_ref: str, claim_ref: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            """SELECT review_id,reviewer,should_push,note,created_at_ms
                 FROM news_notification_feedback
                WHERE decision_ref=%s AND claim_ref=%s
                ORDER BY created_at_ms DESC,review_id DESC LIMIT 1""",
            (decision_ref, claim_ref),
        ).fetchone()
        return None if row is None else dict(row)

    def _open_decision_queue(self, query: DeskQuery) -> dict[str, Any]:
        if query.task:
            decision_ref, claim_ref = _parse_decision_task_id(query.task)
            row = self._decision_row(decision_ref, claim_ref)
            rows = [] if row is None else [row]
            selected = []
            for row in rows:
                accepted = self._decision_feedback(str(row["decision_ref"]), str(row["claim_ref"]))
                task = _decision_task_public(row, accepted)
                if query.stratum and task["selection"]["stratum"] != query.stratum:
                    continue
                if query.status == "pending" and accepted is not None:
                    continue
                if query.status == "accepted" and accepted is None:
                    continue
                selected.append(task)
        else:
            cursor = None
            if query.cursor:
                try:
                    decoded = json.loads(base64.urlsafe_b64decode(query.cursor.encode()))
                    if not isinstance(decoded, list) or len(decoded) != 3:
                        raise ValueError("invalid cursor shape")
                    stamp, decision_ref, claim_ref = decoded
                    if type(stamp) is not int or not isinstance(decision_ref, str) or not isinstance(claim_ref, str):
                        raise ValueError("invalid cursor types")
                    cursor = (stamp, decision_ref, claim_ref)
                except (ValueError, TypeError, binascii.Error) as exc:
                    raise ValueError("news_review_cursor_invalid") from exc
            statement = _decision_queue_statement(
                lower_ms=self._now_ms - query.hours * 3_600_000,
                upper_ms=self._now_ms,
                event=query.event,
                cohort=query.cohort,
                cursor=cursor,
                status=query.status,
                stratum=query.stratum,
                limit=query.limit + 1,
            )
            rows = [dict(row) for row in self._conn.execute(statement.sql, statement.params).fetchall()]
            selected = []
            for row in rows:
                accepted = None
                if row["feedback_review_id"] is not None:
                    accepted = {
                        "review_id": row["feedback_review_id"],
                        "reviewer": row["feedback_reviewer"],
                        "should_push": row["feedback_should_push"],
                        "note": row["feedback_note"],
                        "created_at_ms": row["feedback_created_at_ms"],
                    }
                selected.append(_decision_task_public(row, accepted))
        page = selected[: query.limit]
        next_cursor = None
        if len(selected) > query.limit and page:
            last = page[-1]
            next_cursor = base64.urlsafe_b64encode(
                json.dumps([last["opened_at_ms"], last["decision_ref"], last["claim_ref"]]).encode()
            ).decode()
        counts: dict[str, int] = {}
        for task in page:
            name = task["selection"]["stratum"]
            counts[name] = counts.get(name, 0) + 1
        return {
            "view": "queue",
            "status": "ready" if page else "insufficient_evidence",
            "reader_contract_version": DECISION_READER_CONTRACT_VERSION,
            "rubric_version": DECISION_REVIEW_VERSION,
            "tasks": page,
            "next_cursor": next_cursor,
            "counts": counts,
        }

    def _decision_coverage(self, query: DeskQuery) -> dict[str, Any]:
        statement = _decision_coverage_statement(lower_ms=self._now_ms - query.hours * 3_600_000, upper_ms=self._now_ms)
        row = self._conn.execute(statement.sql, statement.params).fetchone()
        counts = {
            name: int(row[name])
            for name in ("claims", "selected", "feed_only", "reader_unavailable", "sent", "reviewed")
        }
        return {
            "view": "coverage",
            "status": "ready",
            "counts": counts,
            "rubric_version": DECISION_REVIEW_VERSION,
            "reader_contract_version": DECISION_READER_CONTRACT_VERSION,
        }

    def _submit_decision(
        self,
        task: TaskRef,
        submission: DecisionFeedbackSubmission,
        *,
        principal: Principal,
        key: str,
        request_sha: str,
    ) -> dict[str, Any]:
        existing = self._conn.execute(
            """SELECT review_id,request_sha FROM news_notification_feedback
                WHERE reviewer=%s AND idempotency_key=%s""",
            (principal.subject, key),
        ).fetchone()
        if existing is not None:
            if existing["request_sha"] != request_sha:
                raise ValueError("news_review_idempotency_conflict")
            return {
                "idempotent": True,
                "receipt": {
                    "review_id": existing["review_id"],
                    "acceptance_id": existing["review_id"],
                    "task_id": task.task_id,
                    "task_version": task.task_version,
                },
                "next_task": None,
                "updated_queue_counts": {},
            }
        decision_ref, claim_ref = _parse_decision_task_id(task.task_id)
        row = self._decision_row(decision_ref, claim_ref)
        if row is None:
            raise ValueError("news_review_task_not_found")
        if _decision_task_ref(row).task_version != task.task_version:
            raise ValueError("news_review_task_version_conflict")
        review_id = _sha(
            {
                "decision_ref": decision_ref,
                "claim_ref": claim_ref,
                "reviewer": principal.subject,
                "idempotency_key": key,
                "request_sha": request_sha,
            }
        )
        self._conn.execute(
            """INSERT INTO news_notification_feedback
                (review_id,decision_ref,claim_ref,task_version,reviewer,idempotency_key,
                 request_sha,should_push,note,created_at_ms)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (
                review_id,
                decision_ref,
                claim_ref,
                task.task_version,
                principal.subject,
                key,
                request_sha,
                submission.should_push,
                submission.note,
                self._db_now_ms(),
            ),
        )
        return {
            "idempotent": False,
            "receipt": {
                "review_id": review_id,
                "acceptance_id": review_id,
                "task_id": task.task_id,
                "task_version": task.task_version,
            },
            "next_task": None,
            "updated_queue_counts": {},
        }

    def _submit_external(
        self,
        submission: ExternalMissSubmission,
        *,
        principal: Principal,
        idempotency_key: str,
        idempotency_request_sha: str,
    ) -> dict[str, Any]:
        existing = self._conn.execute(
            """SELECT review_id,snapshot_id,created_at_ms,request_sha
                 FROM news_notification_external_feedback
                WHERE reviewer=%s AND idempotency_key=%s""",
            (principal.subject, idempotency_key),
        ).fetchone()
        if existing is not None:
            if existing["request_sha"] != idempotency_request_sha:
                raise ValueError("news_review_idempotency_conflict")
            return {
                "idempotent": True,
                "receipt": {
                    "review_id": existing["review_id"],
                    "acceptance_id": existing["review_id"],
                    "external_snapshot_id": existing["snapshot_id"],
                    "task_id": f"external.{existing['snapshot_id']}",
                    "created_at_ms": existing["created_at_ms"],
                },
                "next_task": None,
                "updated_queue_counts": {},
            }
        created_at = self._db_now_ms()
        if submission.occurred_at_ms > created_at:
            raise ValueError("news_review_external_miss_future")
        evidence = {
            "schema_version": "news_external_miss_v1",
            "source_url": submission.source_url,
            "title": submission.title,
            "body": submission.body,
            "occurred_at_ms": submission.occurred_at_ms,
            "observed_at_ms": created_at,
            # V1 has one authenticated operator principal.  Provenance is a
            # server-owned fact; accepting it from the body would let a caller
            # impersonate a provider, reviewer, or collection path.
            "provenance": "operator_reported",
        }
        evidence_sha = _sha(evidence)
        snapshot_id = _sha({"evidence_sha256": evidence_sha, "creator": principal.subject})
        task_id = f"external.{snapshot_id}"
        feedback = submission.feedback
        review_id = _sha(
            {
                "kind": "notification_external_feedback",
                "snapshot_id": snapshot_id,
                "reviewer": principal.subject,
                "idempotency_key": idempotency_key,
                "request_sha": idempotency_request_sha,
            }
        )
        self._conn.execute(
            """
            INSERT INTO news_external_miss_snapshots (
              snapshot_id, evidence_sha256, source_url, title, body, occurred_at_ms, observed_at_ms,
              provenance, snapshot, created_by, created_at_ms
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
            """,
            (
                snapshot_id,
                evidence_sha,
                submission.source_url,
                submission.title,
                submission.body,
                submission.occurred_at_ms,
                created_at,
                "operator_reported",
                _json(evidence),
                principal.subject,
                created_at,
            ),
        )
        self._conn.execute(
            """
            INSERT INTO news_notification_external_feedback
              (review_id,snapshot_id,reviewer,idempotency_key,request_sha,should_push,note,created_at_ms)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                review_id,
                snapshot_id,
                principal.subject,
                idempotency_key,
                idempotency_request_sha,
                feedback.should_push,
                feedback.note,
                created_at,
            ),
        )
        return {
            "idempotent": False,
            "receipt": {
                "review_id": review_id,
                "acceptance_id": review_id,
                "external_snapshot_id": snapshot_id,
                "task_id": task_id,
                "created_at_ms": created_at,
            },
            "next_task": None,
            "updated_queue_counts": {},
        }

    def _db_now_ms(self) -> int:
        row = self._conn.execute(
            "SELECT floor(extract(epoch FROM clock_timestamp()) * 1000)::bigint AS now_ms"
        ).fetchone()
        return int(row["now_ms"])

    @staticmethod
    def _require_principal(principal: Principal) -> None:
        if not principal.can_review:
            raise PermissionError("news_review_forbidden")


def _idempotency_key(value: str) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > 128:
        raise ValueError("news_review_idempotency_key_invalid")
    return normalized


def _sha(value: Any) -> str:
    return canonical_sha(value)


def _json(value: Any) -> str:
    return canonical_json(value)


def review_read_statements(*, now_ms: int) -> tuple[ReviewReadStatement, ...]:
    """The current decision reads planned by the PostgreSQL query audit."""
    lower = int(now_ms) - 24 * 3_600_000
    return (
        _decision_queue_statement(lower_ms=lower, upper_ms=int(now_ms), limit=101),
        _decision_coverage_statement(lower_ms=lower, upper_ms=int(now_ms)),
        _decision_evidence_statement("notification_decision:audit", "cl:audit"),
    )


__all__ = [
    "DecisionFeedbackSubmission",
    "DeskQuery",
    "ExternalMissSubmission",
    "Principal",
    "ReviewDesk",
    "ReviewReadStatement",
    "ReviewSubmission",
    "TaskRef",
    "review_read_statements",
]
