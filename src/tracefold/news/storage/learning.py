"""Canary, assignment, runtime-manifest, and learning-artifact persistence."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from ..artifact_identity import canonical_sha
from .sql_values import _dumps


class LearningStorage:
    conn: Any

    def active_canary(self) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM news_canary_activations WHERE state = 'active' ORDER BY activated_at_ms DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None

    def validated_active_canary(self, *, now_ms: int) -> dict[str, Any] | None:
        """Trip a durable activation whose code-owned selector identities drifted."""

        from ..learning.canary import (
            CANARY_ELIGIBILITY_PROFILE_SHA,
            CANARY_ROLLING_PROFILE_SHA,
            CANARY_SELECTOR_VERSION,
        )

        activation = self.active_canary()
        if activation is None:
            return None
        expected = (
            ("selector_version", CANARY_SELECTOR_VERSION, "selector_version_mismatch"),
            ("eligibility_profile_sha", CANARY_ELIGIBILITY_PROFILE_SHA, "eligibility_profile_hash_mismatch"),
            ("rolling_profile_sha", CANARY_ROLLING_PROFILE_SHA, "rolling_profile_hash_mismatch"),
        )
        for field_name, value, reason in expected:
            if str(activation.get(field_name) or "") != value:
                self.transition_canary(
                    activation_id=str(activation["activation_id"]),
                    target_state="tripped",
                    reason=reason,
                    now_ms=now_ms,
                )
                return None
        return activation

    def canary_status(self) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM news_canary_activations ORDER BY created_at_ms DESC LIMIT 1").fetchone()
        if row is None:
            return {"state": "inactive", "activation": None, "assignments": {"stable": 0, "candidate": 0}}
        activation = dict(row)
        counts = self.conn.execute(
            """
            SELECT arm, count(*) AS n
              FROM news_agent_assignments
             WHERE activation_id = %s
             GROUP BY arm
            """,
            (activation["activation_id"],),
        ).fetchall()
        return {
            "state": activation["state"],
            "activation": activation,
            "assignments": {str(item["arm"]): int(item["n"]) for item in counts},
        }

    def canary_candidate_eligible(self, candidate_manifest_sha: str) -> bool:
        row = self.conn.execute(
            """
            SELECT 1 AS ok
              FROM news_learning_artifacts
             WHERE kind = 'release_evidence'
               AND payload->>'candidate_sha' = %s
               AND payload->>'stage' = 'shadow'
               AND payload->>'gate_outcome' = 'pass'
             LIMIT 1
            """,
            (candidate_manifest_sha,),
        ).fetchone()
        return bool(row)

    def arm_canary(
        self,
        *,
        activation_id: str,
        baseline_bundle_sha: str,
        candidate_manifest_sha: str,
        candidate_bundle_sha: str,
        selector_version: str,
        exposure_bps: int,
        eligibility_profile_sha: str,
        rolling_profile_sha: str,
        now_ms: int,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO news_canary_activations(
              activation_id, baseline_bundle_sha, candidate_manifest_sha, candidate_bundle_sha,
              selector_version, exposure_bps, eligibility_profile_sha,
              rolling_profile_sha, state, revision, created_at_ms, activated_at_ms
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'active',1,%s,%s)
            """,
            (
                activation_id,
                baseline_bundle_sha,
                candidate_manifest_sha,
                candidate_bundle_sha,
                selector_version,
                int(exposure_bps),
                eligibility_profile_sha,
                rolling_profile_sha,
                int(now_ms),
                int(now_ms),
            ),
        )
        self._append_learning_artifact(
            "deployment_receipt",
            {
                "action": "canary_arm",
                "activation_id": activation_id,
                "baseline_bundle_sha": baseline_bundle_sha,
                "candidate_manifest_sha": candidate_manifest_sha,
                "candidate_bundle_sha": candidate_bundle_sha,
                "selector_version": selector_version,
                "exposure_bps": int(exposure_bps),
                "eligibility_profile_sha": eligibility_profile_sha,
                "rolling_profile_sha": rolling_profile_sha,
                "activated_at_ms": int(now_ms),
            },
            parent_sha=candidate_manifest_sha,
            created_by="canary_control",
            now_ms=now_ms,
        )

    def transition_canary(
        self,
        *,
        activation_id: str,
        target_state: str,
        reason: str,
        now_ms: int,
    ) -> bool:
        if target_state not in {"armed", "active", "tripped", "closed"}:
            raise ValueError("news_canary_transition_invalid")
        row = self.conn.execute(
            "SELECT * FROM news_canary_activations WHERE activation_id = %s FOR UPDATE",
            (activation_id,),
        ).fetchone()
        if row is None:
            raise ValueError("news_canary_activation_not_found")
        if row["state"] in {"tripped", "closed"}:
            return False
        allowed_sources = {
            "armed": {"active"},
            "active": {"armed"},
            "tripped": {"armed", "active"},
            "closed": {"armed", "active"},
        }[target_state]
        if str(row["state"]) not in allowed_sources:
            return False
        stamp_column = {
            "armed": "held_at_ms",
            "active": "resumed_at_ms",
            "tripped": "tripped_at_ms",
            "closed": "closed_at_ms",
        }[target_state]
        reason_column = "hold_reason" if target_state in {"armed", "active"} else "trip_reason"
        cursor = self.conn.execute(
            f"""
            UPDATE news_canary_activations
               SET state = %s, revision = revision + 1, {reason_column} = %s, {stamp_column} = %s
             WHERE activation_id = %s AND revision = %s AND state = %s
            """,
            (
                target_state,
                reason[:200],
                int(now_ms),
                activation_id,
                int(row["revision"]),
                row["state"],
            ),
        )
        changed = bool(cursor.rowcount)
        if changed:
            kind = "rollback_receipt" if target_state == "tripped" else "deployment_receipt"
            action = {
                "armed": "canary_hold",
                "active": "canary_resume",
                "tripped": "canary_trip",
                "closed": "canary_close",
            }[target_state]
            self._append_learning_artifact(
                kind,
                {
                    "action": action,
                    "activation_id": activation_id,
                    "baseline_bundle_sha": str(row["baseline_bundle_sha"]),
                    "candidate_manifest_sha": str(row["candidate_manifest_sha"]),
                    "candidate_bundle_sha": str(row["candidate_bundle_sha"]),
                    "reason": reason[:200],
                    "transitioned_at_ms": int(now_ms),
                    "previous_revision": int(row["revision"]),
                    "new_revision": int(row["revision"]) + 1,
                },
                parent_sha=str(row["candidate_manifest_sha"]),
                created_by="canary_control",
                now_ms=now_ms,
            )
        return changed

    def evaluate_canary_rolling_slo(self, *, activation_id: str, now_ms: int) -> dict[str, Any]:
        """Evaluate one durable, pre-registered rolling candidate SLO bucket."""

        from ..learning.canary import (
            CANARY_ELIGIBILITY_PROFILE_SHA,
            CANARY_ROLLING_PROFILE,
            CANARY_ROLLING_PROFILE_SHA,
            CANARY_SELECTOR_VERSION,
        )

        row = self.conn.execute(
            "SELECT * FROM news_canary_activations WHERE activation_id = %s FOR UPDATE",
            (activation_id,),
        ).fetchone()
        if row is None or str(row["state"]) != "active":
            return {"evaluated": False, "reason": "activation_not_active"}
        identity_checks = (
            ("selector_version", CANARY_SELECTOR_VERSION, "selector_version_mismatch"),
            ("eligibility_profile_sha", CANARY_ELIGIBILITY_PROFILE_SHA, "eligibility_profile_hash_mismatch"),
            ("rolling_profile_sha", CANARY_ROLLING_PROFILE_SHA, "rolling_profile_hash_mismatch"),
        )
        for field_name, value, reason in identity_checks:
            if str(row[field_name]) != value:
                self.transition_canary(
                    activation_id=activation_id,
                    target_state="tripped",
                    reason=reason,
                    now_ms=now_ms,
                )
                return {"evaluated": True, "tripped": True, "reason": reason}
        bucket_ms = int(CANARY_ROLLING_PROFILE["evaluation_bucket_ms"])
        bucket = int(now_ms) // bucket_ms * bucket_ms
        if row["rolling_last_bucket_ms"] is not None and int(row["rolling_last_bucket_ms"]) >= bucket:
            return {"evaluated": False, "reason": "bucket_already_evaluated"}
        lower = bucket - int(CANARY_ROLLING_PROFILE["lookback_ms"])
        counts = self.conn.execute(
            """
            SELECT count(*) AS n,
                   count(*) FILTER (
                     WHERE v.present IS NULL OR v.degraded OR v.error_code IS NOT NULL
                   ) AS bad_n
              FROM news_agent_assignments a
              LEFT JOIN LATERAL (
                SELECT true AS present, x.degraded, x.error_code
                  FROM news_verdicts x
                 WHERE x.event_id = a.event_id AND x.stage = 'triage'
                 ORDER BY x.created_at_ms DESC LIMIT 1
              ) v ON true
             WHERE a.activation_id = %s AND a.arm = 'candidate'
               AND a.assigned_at_ms >= %s AND a.assigned_at_ms < %s
            """,
            (activation_id, lower, bucket),
        ).fetchone()
        n = int(counts["n"] or 0)
        bad_n = int(counts["bad_n"] or 0)
        enough = n >= int(CANARY_ROLLING_PROFILE["candidate_min_n"])
        breached = enough and bad_n / n > float(CANARY_ROLLING_PROFILE["error_or_degraded_rate_max"])
        breach_windows = int(row["rolling_breach_windows"] or 0) + 1 if breached else 0
        self.conn.execute(
            """
            UPDATE news_canary_activations
               SET rolling_last_bucket_ms = %s, rolling_breach_windows = %s,
                   revision = revision + 1
             WHERE activation_id = %s AND revision = %s AND state = 'active'
            """,
            (bucket, breach_windows, activation_id, int(row["revision"])),
        )
        tripped = breach_windows >= int(CANARY_ROLLING_PROFILE["consecutive_breach_buckets"])
        if tripped:
            self.transition_canary(
                activation_id=activation_id,
                target_state="tripped",
                reason="candidate_rolling_error_slo_trip",
                now_ms=now_ms,
            )
        return {
            "evaluated": True,
            "bucket_ms": bucket,
            "candidate_n": n,
            "bad_n": bad_n,
            "breached": breached,
            "breach_windows": breach_windows,
            "tripped": tripped,
        }

    def assign_agent_arm(
        self,
        *,
        event_id: str,
        stable_bundle_sha: str,
        admission: str,
        ingest_mode: str,
        now_ms: int,
    ) -> dict[str, Any]:
        existing = self.conn.execute("SELECT * FROM news_agent_assignments WHERE event_id = %s", (event_id,)).fetchone()
        if existing:
            return self._validated_existing_agent_assignment(
                existing,
                stable_bundle_sha=stable_bundle_sha,
                now_ms=now_ms,
            )
        activation = self.validated_active_canary(now_ms=now_ms)
        if activation is None:
            selection = {
                "activation_id": None,
                "arm": "stable",
                "bundle_sha": stable_bundle_sha,
                "selector_version": "stable_only_v2",
                "eligibility_reason": "no_active_canary",
            }
        elif str(activation["baseline_bundle_sha"]) != stable_bundle_sha:
            self.transition_canary(
                activation_id=str(activation["activation_id"]),
                target_state="tripped",
                reason="baseline_bundle_mismatch",
                now_ms=now_ms,
            )
            selection = {
                "activation_id": str(activation["activation_id"]),
                "arm": "stable",
                "bundle_sha": stable_bundle_sha,
                "selector_version": str(activation["selector_version"]),
                "eligibility_reason": "activation_tripped_baseline_mismatch",
            }
        else:
            from ..learning.canary import select_canary_arm

            selected = select_canary_arm(
                event_id=event_id,
                activation_id=str(activation["activation_id"]),
                baseline_bundle_sha=stable_bundle_sha,
                candidate_bundle_sha=str(activation["candidate_bundle_sha"]),
                exposure_bps=int(activation["exposure_bps"]),
                admission=admission,
                ingest_mode=ingest_mode,
            )
            selection = {
                "activation_id": str(activation["activation_id"]),
                "arm": selected.arm,
                "bundle_sha": selected.bundle_sha,
                "selector_version": str(activation["selector_version"]),
                "eligibility_reason": selected.eligibility_reason,
            }
        self.conn.execute(
            """
            INSERT INTO news_agent_assignments(
              event_id, activation_id, arm, bundle_sha, selector_version,
              eligibility_reason, assigned_at_ms
            ) VALUES (%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (event_id) DO NOTHING
            """,
            (
                event_id,
                selection["activation_id"],
                selection["arm"],
                selection["bundle_sha"],
                selection["selector_version"],
                selection["eligibility_reason"],
                int(now_ms),
            ),
        )
        row = self.conn.execute("SELECT * FROM news_agent_assignments WHERE event_id = %s", (event_id,)).fetchone()
        if row is None:
            raise RuntimeError("news_agent_assignment_insert_failed")
        return dict(row)

    def _validated_existing_agent_assignment(
        self,
        existing: Mapping[str, Any],
        *,
        stable_bundle_sha: str,
        now_ms: int,
    ) -> dict[str, Any]:
        """Revalidate the immutable assignment before a retry executes it.

        The assignment row remains the audit fact.  If its selector generation
        or activation identities no longer match this binary, the returned
        projection carries a validation error so Triage degrades without ever
        executing the stale candidate Program.
        """

        from ..learning.canary import (
            CANARY_ELIGIBILITY_PROFILE_SHA,
            CANARY_ROLLING_PROFILE_SHA,
            CANARY_SELECTOR_VERSION,
        )

        assignment = dict(existing)
        activation_id = assignment.get("activation_id")
        if activation_id is None:
            if (
                str(assignment.get("arm") or "") != "stable"
                or str(assignment.get("bundle_sha") or "") != stable_bundle_sha
                or str(assignment.get("selector_version") or "") != "stable_only_v2"
            ):
                assignment["validation_error"] = "stable_assignment_identity_mismatch"
            return assignment

        activation = self.conn.execute(
            "SELECT * FROM news_canary_activations WHERE activation_id = %s",
            (str(activation_id),),
        ).fetchone()
        if activation is None:
            assignment["validation_error"] = "candidate_activation_missing"
            return assignment

        reason: str | None = None
        for field_name, expected, mismatch in (
            ("selector_version", CANARY_SELECTOR_VERSION, "selector_version_mismatch"),
            ("eligibility_profile_sha", CANARY_ELIGIBILITY_PROFILE_SHA, "eligibility_profile_hash_mismatch"),
            ("rolling_profile_sha", CANARY_ROLLING_PROFILE_SHA, "rolling_profile_hash_mismatch"),
        ):
            if str(activation[field_name]) != expected:
                reason = mismatch
                break
        if reason is None and str(assignment.get("selector_version") or "") != CANARY_SELECTOR_VERSION:
            reason = "assignment_selector_version_mismatch"
        if reason is None and str(activation["baseline_bundle_sha"]) != stable_bundle_sha:
            reason = "baseline_bundle_mismatch"
        arm = str(assignment.get("arm") or "")
        bundle_sha = str(assignment.get("bundle_sha") or "")
        if reason is None and arm == "candidate" and bundle_sha != str(activation["candidate_bundle_sha"]):
            reason = "candidate_bundle_mismatch"
        if reason is None and arm == "stable" and bundle_sha != stable_bundle_sha:
            reason = "stable_bundle_mismatch"
        if reason is None and arm not in {"stable", "candidate"}:
            reason = "assignment_arm_invalid"
        if reason is None:
            return assignment

        self.transition_canary(
            activation_id=str(activation_id),
            target_state="tripped",
            reason=reason,
            now_ms=now_ms,
        )
        assignment["validation_error"] = reason
        return assignment

    def register_agent_runtime_manifest(
        self,
        *,
        manifest_sha: str,
        stable_bundle_sha: str,
        candidate_shas: Sequence[str],
        image_digest: str,
        runtime_revision: str,
        now_ms: int,
    ) -> None:
        previous_active = self.conn.execute(
            "SELECT artifact_sha, payload FROM news_learning_artifacts "
            "WHERE kind = 'active_agent' ORDER BY created_at_ms DESC LIMIT 1"
        ).fetchone()
        self.conn.execute(
            """
            INSERT INTO news_agent_runtime_manifests(
              manifest_sha, stable_bundle_sha, candidate_shas, image_digest,
              runtime_revision, registered_at_ms
            ) VALUES (%s,%s,%s::jsonb,%s,%s,%s)
            ON CONFLICT (manifest_sha) DO NOTHING
            """,
            (
                manifest_sha,
                stable_bundle_sha,
                _dumps(sorted(set(candidate_shas))),
                image_digest,
                runtime_revision,
                int(now_ms),
            ),
        )
        previous_payload = dict(previous_active["payload"] or {}) if previous_active else {}
        if previous_payload.get("runtime_manifest_sha") == manifest_sha:
            return
        active_sha = self._append_learning_artifact(
            "active_agent",
            {
                "stable_sha": stable_bundle_sha,
                "runtime_manifest_sha": manifest_sha,
                "candidate_shas": sorted(set(candidate_shas)),
                "image_digest": image_digest,
                "runtime_revision": runtime_revision,
                "registered_at_ms": int(now_ms),
            },
            parent_sha=str(previous_active["artifact_sha"]) if previous_active else None,
            created_by="worker_startup",
            now_ms=now_ms,
        )
        previous_stable = str(previous_payload["stable_sha"]) if previous_payload else None
        previous_image = str(previous_payload["image_digest"]) if previous_payload else None
        self._append_learning_artifact(
            "deployment_receipt",
            {
                "action": "runtime_deploy",
                "active_agent_sha": active_sha,
                "stable_sha": stable_bundle_sha,
                "image_digest": image_digest,
                "runtime_revision": runtime_revision,
                "previous_stable_sha": previous_stable,
                "previous_image_digest": previous_image,
                "deployed_at_ms": int(now_ms),
                "rollback_available_until_ms": int(now_ms) + 24 * 3_600_000,
            },
            parent_sha=active_sha,
            created_by="worker_startup",
            now_ms=now_ms,
        )

    def append_proposal_artifact(
        self,
        *,
        kind: str,
        payload: Mapping[str, Any],
        parent_sha: str | None,
        created_at_ms: int,
    ) -> str:
        """The `learning_propose` writer: content-addressed, then read back before it counts.

        Deliberately not `_append_learning_artifact`. A proposal document is arbitrary operator or
        optimizer JSON, so it is normalized through `json` with `default=str` and addressed with
        `canonical_sha`; and the row is re-read, because two different documents landing on one
        `artifact_sha` would otherwise make the second silently invisible instead of failing the
        registration that a receipt chain later verifies.
        """

        public = json.loads(json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, default=str))
        artifact_sha = canonical_sha({"kind": kind, "payload": public})
        self.conn.execute(
            "INSERT INTO news_learning_artifacts "
            "(artifact_sha, kind, parent_sha, payload, created_by, created_at_ms) "
            "VALUES (%s, %s, %s, %s::jsonb, 'learning_propose', %s) "
            "ON CONFLICT (artifact_sha) DO NOTHING",
            (artifact_sha, kind, parent_sha, json.dumps(public, ensure_ascii=False, sort_keys=True), created_at_ms),
        )
        row = self.conn.execute(
            "SELECT kind, payload FROM news_learning_artifacts WHERE artifact_sha = %s",
            (artifact_sha,),
        ).fetchone()
        if row is None or str(row["kind"]) != kind or dict(row["payload"] or {}) != public:
            raise ValueError("news_learning_artifact_collision")
        return artifact_sha

    def _append_learning_artifact(
        self,
        kind: str,
        payload: Mapping[str, Any],
        *,
        parent_sha: str | None,
        created_by: str,
        now_ms: int,
    ) -> str:
        public = dict(payload)
        artifact_sha = hashlib.sha256(_dumps({"kind": kind, "payload": public}).encode("utf-8")).hexdigest()
        self.conn.execute(
            """
            INSERT INTO news_learning_artifacts(
              artifact_sha, kind, parent_sha, payload, created_by, created_at_ms
            ) VALUES (%s,%s,%s,%s::jsonb,%s,%s)
            ON CONFLICT (artifact_sha) DO NOTHING
            """,
            (artifact_sha, kind, parent_sha, _dumps(public), created_by, int(now_ms)),
        )
        return artifact_sha
