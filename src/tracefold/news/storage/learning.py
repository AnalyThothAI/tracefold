"""Canary, assignment, runtime-manifest, and learning-artifact persistence."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from ..artifact_identity import canonical_sha
from ..learning.contracts import epoch_id_for_bundle
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

        from ..release.canary import (
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

        from ..release.canary import (
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
            from ..release.canary import select_canary_arm

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

        from ..release.canary import (
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

    def open_learning_epoch(
        self,
        *,
        bundle_sha: str,
        envelope_sha256: str,
        artifact_schema_version: str,
        program_version: str,
        program_sha256: str,
        now_ms: int,
    ) -> bool:
        """Open this bundle's evidence epoch if it has never run here, and say whether it was new.

        This is what migrations `0292`-`0319` used to do by hand, one per identity change (#314). Doing it
        at startup instead removes the failure mode those migrations had: an author who changed behavior and
        did not think to write one. The append-only trigger and the primary key make a concurrent or repeated
        start idempotent rather than merely unlikely — the second writer loses the insert and reads `False`.

        `starts_at_ms` is strictly after every epoch already recorded. Evidence eligibility is a timestamp
        comparison, so an epoch that opened at or before its predecessor would admit that predecessor's
        verdicts into this cohort.

        Known limitation, deliberately not fixed here (#314 review). Deploying A, then B, then A again — a
        redeploy of an earlier image is the only rollback there is — leaves A's row at its original start,
        because the table is append-only and the row cannot move. A reader that filters on `bundle_sha`
        is unaffected and that is most of them; a reader that can only compare timestamps, notably
        external-miss eligibility (an external miss has no bundle to filter on), will admit evidence
        produced while B was live into A's cohort. Stating the shape of the fix so it is not re-derived:
        a cohort is really the union of the intervals during which its bundle was the appointed Agent, and
        `news_learning_artifacts(kind='active_agent')` already records every appointment with a timestamp.
        Until a reader consults that history, one lower bound per epoch is an approximation that is exact
        for a forward-only deployment sequence and generous for a rollback. It is strictly better than what
        it replaced, where every bundle shared one hand-declared epoch and no rollback registered at all.
        """

        row = self.conn.execute(
            """
            INSERT INTO news_learning_epochs (
              epoch_id, starts_at_ms, source_issue, bundle_sha, envelope_sha256, artifact_schema_version,
              baseline_program_version, baseline_program_sha256, prior_evidence_disposition,
              reset_reason, created_at_ms
            )
            SELECT %s,
                   greatest(%s, coalesce(max(starts_at_ms), 0) + 1),
                   'https://github.com/AnalyThothAI/tracefold/issues/314',
                   %s, %s, %s, %s, %s, 'audit_only', 'runtime_bundle_identity_change',
                   greatest(%s, coalesce(max(starts_at_ms), 0) + 1)
              FROM news_learning_epochs
            ON CONFLICT (epoch_id) DO NOTHING
            RETURNING epoch_id
            """,
            (
                epoch_id_for_bundle(bundle_sha),
                int(now_ms),
                bundle_sha,
                envelope_sha256,
                artifact_schema_version,
                program_version,
                program_sha256,
                int(now_ms),
            ),
        ).fetchone()
        if row is None:
            # The insert lost. Either this bundle already has its epoch — the ordinary restart — or an
            # unrelated bundle already holds the eight-hex label this one abbreviates to. The second is
            # vanishingly unlikely and completely silent if left alone: the freeze that needed the epoch
            # would fail hours later as `news_learning_epoch_not_deployed`, naming the wrong problem. So
            # the barrier reads back what it lost to and refuses to start if it is not this bundle's.
            existing = self.conn.execute(
                "SELECT bundle_sha FROM news_learning_epochs WHERE epoch_id = %s",
                (epoch_id_for_bundle(bundle_sha),),
            ).fetchone()
            if existing is None or str(existing["bundle_sha"] or "") != bundle_sha:
                raise ValueError("news_learning_epoch_id_collision")
            return False
        # A canary compares a candidate against the stable arm it was registered under. That arm no longer
        # runs here, so every nonterminal activation is comparing against something absent — the same
        # reasoning, and the same trip, that every hand-written epoch migration carried.
        status = self.canary_status()
        activation = status.get("activation")
        if activation is not None and str(activation["state"]) in {"armed", "active"}:
            self.transition_canary(
                activation_id=str(activation["activation_id"]),
                target_state="tripped",
                reason="learning_epoch_opened",
                now_ms=int(now_ms),
            )
        return True

    def register_agent_runtime_manifest(
        self,
        *,
        manifest_sha: str,
        stable_bundle_sha: str,
        envelope_sha256: str,
        artifact_schema_version: str,
        program_version: str,
        program_sha256: str,
        candidate_shas: Sequence[str],
        image_digest: str,
        runtime_revision: str,
        now_ms: int,
    ) -> None:
        """Appoint the running Agent, opening its evidence epoch first.

        The epoch is opened before the manifest short-circuit below, not after: an unchanged manifest is the
        normal restart, and a restart must still leave a database that lost the row — or never had it —
        with an epoch for the bundle that is running.
        """

        self.open_learning_epoch(
            bundle_sha=stable_bundle_sha,
            envelope_sha256=envelope_sha256,
            artifact_schema_version=artifact_schema_version,
            program_version=program_version,
            program_sha256=program_sha256,
            now_ms=now_ms,
        )
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
        # A Prompt candidate already has a root, and that root is what the registration receipt names.
        # Wrapping it in `sha({kind, payload})` would give one object two addresses and force every reader
        # to know both; the row is instead stored under the identity the document carries, and the
        # read-back below still refuses a second document landing on it. `compile_record` keeps the same
        # rule so pre-#202 rows stay resolvable as audit history.
        self_addressed = {"prompt_candidate": "candidate_sha256", "compile_record": "compile_record_sha256"}
        own_root = self_addressed.get(kind)
        artifact_sha = (
            str(public[own_root])
            if own_root is not None and isinstance(public.get(own_root), str)
            else canonical_sha({"kind": kind, "payload": public})
        )
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

    def candidate_registered_at_ms(self, candidate_sha: str) -> int | None:
        """When this candidate first entered the ledger, or None when it never did."""

        row = self.conn.execute(
            "SELECT created_at_ms FROM news_learning_artifacts "
            "WHERE kind = 'candidate' AND payload ->> 'candidate_sha' = %s "
            "ORDER BY created_at_ms LIMIT 1",
            (candidate_sha,),
        ).fetchone()
        return None if row is None else int(row["created_at_ms"])

    def candidate_passed_stage(self, candidate_sha: str, stage: str) -> bool:
        row = self.conn.execute(
            """
            SELECT 1 AS ok
              FROM news_learning_artifacts
             WHERE kind = 'release_evidence'
               AND payload->>'candidate_sha' = %s
               AND payload->>'stage' = %s
               AND payload->>'gate_outcome' = 'pass'
             LIMIT 1
            """,
            (candidate_sha, stage),
        ).fetchone()
        return bool(row)

    def learning_artifact(self, artifact_sha: str, *, kind: str | None = None) -> dict[str, Any] | None:
        """One artifact row by its address, optionally required to be of a given kind.

        Returns the row rather than the payload: three callers need `kind` back to refuse a document that
        answers to the right address under the wrong name, which is the case a payload-only read cannot
        distinguish from a missing row.
        """

        if kind is None:
            row = self.conn.execute(
                "SELECT artifact_sha, kind, parent_sha, payload FROM news_learning_artifacts WHERE artifact_sha = %s",
                (artifact_sha,),
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT artifact_sha, kind, parent_sha, payload FROM news_learning_artifacts "
                "WHERE artifact_sha = %s AND kind = %s",
                (artifact_sha, kind),
            ).fetchone()
        return None if row is None else dict(row)

    def append_model_recording(self, values: Sequence[Any]) -> None:
        """Append one exact Predictor call recording.

        The column list is the contract: 26 values in a fixed order, which is why this takes the tuple the
        caller already assembled rather than 26 keyword arguments that would only be re-packed here. The
        caller reads the row back through `model_recording` and compares field by field, so a silent
        `ON CONFLICT DO NOTHING` cannot pass for a write.
        """

        self.conn.execute(
            """
    INSERT INTO news_model_recordings (
      recording_sha, run_sha, case_id, arm, trial, predictor_name, call_index, attempt, route,
      request_sha256, response_sha256, request, response, provider, model, model_sha,
      execution_contract_sha, latency_ms, input_tokens, output_tokens, cached_tokens, total_tokens,
      provider_cost_microusd, finish_reason, error_code, created_at_ms
    ) VALUES (
      %s, %s, %s, %s, %s, %s, %s, %s, %s,
      %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s,
      %s, %s, %s, %s, %s, %s,
      %s, %s, %s, %s
    ) ON CONFLICT DO NOTHING
    """,
            tuple(values),
        )

    def accepted_event_review_sources(
        self, *, rubric_versions: Sequence[str], reader_contract_version: str, from_ms: int, to_ms: int
    ) -> list[dict[str, Any]]:
        """Every accepted event review whose Event opened in the window, with no cohort filter. Baseline-only."""

        rows = self.conn.execute(
            """
    WITH accepted AS (
      SELECT DISTINCT ON (j.event_id) j.*
        FROM news_reviews a
        JOIN news_reviews j ON j.review_id = a.accepts_review_id
       WHERE a.review_kind = 'acceptance' AND j.subject_kind = 'event'
         AND a.release_eligible AND j.release_eligible
         AND j.rubric_version = ANY(%s) AND j.reader_contract_version = %s
       ORDER BY j.event_id, a.created_at_ms DESC, a.review_id DESC
    )
    SELECT accepted.*, source.evidence_sha256, source.opened_at_ms,
           source.final_decision, source.delivery_state, source.evidence_snapshot
      FROM accepted
      JOIN news_review_task_source_v1 source
        ON source.event_id = accepted.event_id
       AND source.evidence_version = accepted.evidence_version
     WHERE source.opened_at_ms >= %s AND source.opened_at_ms < %s AND source.ingest_mode = 'live'
    """,
            (list(rubric_versions), reader_contract_version, from_ms, to_ms),
        ).fetchall()
        return [dict(row) for row in rows]

    def accepted_event_reviews_in_window(
        self,
        *,
        epoch_started_at_ms: int,
        freeze_as_of_ms: int,
        rubric_versions: Sequence[str],
        reader_contract_version: str,
        from_ms: int,
        to_ms: int,
        program_version: str,
        program_sha256: str,
        policy_version: str,
        bundle_sha: str,
    ) -> list[dict[str, Any]]:
        """Accepted event reviews in the current cohort, inside one closed window."""

        rows = self.conn.execute(
            """
    WITH accepted AS (
      SELECT DISTINCT ON (j.event_id) j.*, a.created_at_ms AS accepted_at_ms
        FROM news_reviews a
        JOIN news_reviews j ON j.review_id = a.accepts_review_id
       WHERE a.review_kind = 'acceptance' AND j.subject_kind = 'event'
         AND a.release_eligible AND j.release_eligible
         AND a.created_at_ms >= %s AND j.created_at_ms >= %s
         AND a.created_at_ms <= %s AND j.rubric_version = ANY(%s)
         AND j.reader_contract_version = %s
       ORDER BY j.event_id, a.created_at_ms DESC, a.review_id DESC
    )
    SELECT accepted.*, source.evidence_sha256, source.opened_at_ms,
           source.final_decision, source.delivery_state, source.evidence_release_eligible,
           source.evidence_snapshot
      FROM accepted
      JOIN news_review_task_source_v1 source
        ON source.event_id = accepted.event_id
       AND source.evidence_version = accepted.evidence_version
     WHERE source.opened_at_ms >= %s AND source.opened_at_ms < %s
       AND source.ingest_mode = 'live' AND source.evidence_release_eligible
       AND source.program_version = %s AND source.program_sha256 = %s
       AND source.policy_version = %s
       AND source.trace #>> '{agent_assignment,bundle_sha}' = %s
       AND NOT (
         source.final_decision IN ('push', 'escalate')
         AND COALESCE(source.delivery_state, '') NOT IN ('sent', 'terminal')
       )
       AND (
         source.delivery_state = 'terminal'
         AND source.delivery_error_code = 'ambiguous_after_crash'
       ) IS NOT TRUE
    """,
            (
                epoch_started_at_ms,
                epoch_started_at_ms,
                freeze_as_of_ms,
                list(rubric_versions),
                reader_contract_version,
                from_ms,
                to_ms,
                program_version,
                program_sha256,
                policy_version,
                bundle_sha,
            ),
        ).fetchall()
        return [dict(row) for row in rows]

    def accepted_external_miss_reviews_in_window(
        self,
        *,
        epoch_started_at_ms: int,
        freeze_as_of_ms: int,
        rubric_versions: Sequence[str],
        reader_contract_version: str,
        from_ms: int,
        to_ms: int,
    ) -> list[dict[str, Any]]:
        """Accepted external misses in the current cohort, inside one closed window."""

        rows = self.conn.execute(
            """
    SELECT DISTINCT ON (j.external_snapshot_id) j.*, a.created_at_ms AS accepted_at_ms,
           x.evidence_sha256, x.occurred_at_ms AS opened_at_ms, x.snapshot AS evidence_snapshot
      FROM news_reviews a
      JOIN news_reviews j ON j.review_id = a.accepts_review_id
      JOIN news_external_miss_snapshots x ON x.snapshot_id = j.external_snapshot_id
     WHERE a.review_kind = 'acceptance' AND j.subject_kind = 'external_miss'
       AND a.release_eligible AND j.release_eligible
       AND a.created_at_ms >= %s AND j.created_at_ms >= %s
       AND a.created_at_ms <= %s AND j.rubric_version = ANY(%s)
       AND j.reader_contract_version = %s
       AND x.created_at_ms >= %s
       AND x.occurred_at_ms >= %s AND x.occurred_at_ms < %s
     ORDER BY j.external_snapshot_id, a.created_at_ms DESC, a.review_id DESC
    """,
            (
                epoch_started_at_ms,
                epoch_started_at_ms,
                freeze_as_of_ms,
                list(rubric_versions),
                reader_contract_version,
                epoch_started_at_ms,
                from_ms,
                to_ms,
            ),
        ).fetchall()
        return [dict(row) for row in rows]

    def eligible_stable_arm_event_count(
        self,
        *,
        from_ms: int,
        to_ms: int,
        program_version: str,
        program_sha256: str,
        policy_version: str,
        bundle_sha: str,
    ) -> dict[str, Any] | None:
        """How many live Events the stable arm answered in the window, whether or not anyone reviewed them."""

        row = self.conn.execute(
            "SELECT count(*) AS n FROM news_review_task_source_v1 "
            "WHERE opened_at_ms >= %s AND opened_at_ms < %s AND ingest_mode = 'live' "
            "AND program_version = %s AND program_sha256 = %s AND policy_version = %s "
            "AND trace #>> '{agent_assignment,bundle_sha}' = %s",
            (from_ms, to_ms, program_version, program_sha256, policy_version, bundle_sha),
        ).fetchone()
        return None if row is None else dict(row)

    def newest_canary_activation_for_candidate(self, candidate_manifest_sha: str) -> dict[str, Any] | None:
        """The most recent activation this candidate was ever armed under."""

        row = self.conn.execute(
            "SELECT * FROM news_canary_activations "
            "WHERE candidate_manifest_sha = %s ORDER BY created_at_ms DESC LIMIT 1",
            (candidate_manifest_sha,),
        ).fetchone()
        return None if row is None else dict(row)

    def canary_arm_observations(self, *, activation_id: str, from_ms: int, to_ms: int) -> list[dict[str, Any]]:
        """Every assignment one activation made in the window, with the verdict and delivery it produced."""

        rows = self.conn.execute(
            """
    SELECT a.arm, a.bundle_sha, a.selector_version, a.eligibility_reason,
           a.assigned_at_ms, e.event_id, e.opened_at_ms,
           s.evidence_version, s.evidence_sha256, s.snapshot AS evidence_snapshot,
           v.verdict, v.editorial, v.scored_judgment_sha256, v.runtime_manifest_sha,
           v.final_decision, v.degraded, v.error_code AS verdict_error_code,
           v.trace, v.program_version, v.program_sha256,
           d.state AS delivery_state, d.error_code AS delivery_error_code, d.settled_at_ms
      FROM news_agent_assignments a
      JOIN news_events e ON e.event_id = a.event_id
      LEFT JOIN LATERAL (
        SELECT x.* FROM news_verdicts x
         WHERE x.event_id = e.event_id AND x.stage = 'triage'
         ORDER BY x.created_at_ms DESC LIMIT 1
      ) v ON true
      LEFT JOIN LATERAL (
        SELECT x.* FROM news_event_evidence_snapshots x
         WHERE x.event_id = e.event_id
           AND x.evidence_version = COALESCE(
             v.evidence_version,
             (SELECT max(z.evidence_version) FROM news_event_evidence_snapshots z
               WHERE z.event_id = e.event_id)
           )
      ) s ON true
      LEFT JOIN news_deliveries d ON d.event_id = e.event_id AND d.kind = 'first'
     WHERE a.activation_id = %s
       AND e.opened_at_ms >= %s AND e.opened_at_ms < %s
     ORDER BY e.opened_at_ms, e.event_id
    """,
            (activation_id, from_ms, to_ms),
        ).fetchall()
        return [dict(row) for row in rows]

    def model_recording(self, recording_sha: str) -> dict[str, Any] | None:
        """One persisted Predictor call, read back so the writer can prove what landed."""

        row = self.conn.execute(
            """
    SELECT recording_sha, run_sha, case_id, arm, trial, predictor_name, call_index, attempt, route,
           request_sha256, response_sha256, request, response, provider, model, model_sha,
           execution_contract_sha, latency_ms, input_tokens, output_tokens, cached_tokens, total_tokens,
           provider_cost_microusd, finish_reason, error_code
      FROM news_model_recordings
     WHERE recording_sha = %s
    """,
            (recording_sha,),
        ).fetchone()
        return None if row is None else dict(row)

    def accepted_pairwise_judgments(self, run_sha: str) -> list[dict[str, Any]]:
        """The accepted blind pairwise judgments for one evaluation run."""

        rows = self.conn.execute(
            """
    SELECT DISTINCT ON (j.pairwise_case_id) j.pairwise_case_id, j.payload
      FROM news_reviews a
      JOIN news_reviews j ON j.review_id = a.accepts_review_id
     WHERE a.review_kind = 'acceptance' AND j.subject_kind = 'pairwise'
       AND j.pairwise_case_id LIKE %s
     ORDER BY j.pairwise_case_id, a.created_at_ms DESC, a.review_id DESC
    """,
            (f"{run_sha}:%",),
        ).fetchall()
        return [dict(row) for row in rows]

    def pairwise_review_budget_used(self, run_sha: str) -> dict[str, Any] | None:
        """How many pairwise judgments this run has already spent from the review budget."""

        row = self.conn.execute(
            "SELECT count(*) AS n FROM news_reviews "
            "WHERE review_kind = 'judgment' AND subject_kind = 'pairwise' AND pairwise_case_id LIKE %s",
            (f"{run_sha}:%",),
        ).fetchone()
        return None if row is None else dict(row)

    def stable_arm_review_sources(
        self,
        *,
        from_ms: int,
        to_ms: int,
        bundle_sha: str,
        program_version: str,
        program_sha256: str,
    ) -> list[dict[str, Any]]:
        """Every admitted, release-eligible Event the stable arm answered inside a closed window."""

        rows = self.conn.execute(
            """
            SELECT *
              FROM news_review_task_source_v1
             WHERE opened_at_ms >= %s AND opened_at_ms < %s
               AND ingest_mode = 'live'
               AND admission IN ('candidate', 'listing_deterministic')
               AND evidence_release_eligible
               AND verdict IS NOT NULL
               AND trace #>> '{agent_assignment,arm}' = 'stable'
               AND trace #>> '{agent_assignment,bundle_sha}' = %s
               AND program_version = %s AND program_sha256 = %s
             ORDER BY opened_at_ms, event_id, evidence_version
            """,
            (from_ms, to_ms, bundle_sha, program_version, program_sha256),
        ).fetchall()
        return [dict(row) for row in rows]

    def newest_observation_manifest(self, *, kind: str, observation_run_sha: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT artifact_sha, payload FROM news_learning_artifacts "
            "WHERE kind = %s AND payload->>'observation_run_sha' = %s "
            "ORDER BY created_at_ms DESC LIMIT 1",
            (kind, observation_run_sha),
        ).fetchone()
        return None if row is None else dict(row)

    def review(self, review_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM news_reviews WHERE review_id = %s", (review_id,)).fetchone()
        return None if row is None else dict(row)

    def review_task_source(self, *, event_id: str, evidence_version: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM news_review_task_source_v1 WHERE event_id = %s AND evidence_version = %s",
            (event_id, evidence_version),
        ).fetchone()
        return None if row is None else dict(row)

    def external_miss_snapshot(self, snapshot_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM news_external_miss_snapshots WHERE snapshot_id = %s", (snapshot_id,)
        ).fetchone()
        return None if row is None else dict(row)

    def recorded_triage_decisions(self, event_ids: Sequence[str]) -> list[dict[str, Any]]:
        """The newest persisted triage verdict per Event, with the watchlist hits the Event carried."""

        if not event_ids:
            return []
        rows = self.conn.execute(
            """
            SELECT DISTINCT ON (v.event_id)
                   v.event_id, v.final_decision, v.override_rule, v.throttled_by,
                   v.rule_baseline_decision, v.trace, e.watchlist_hits
              FROM news_verdicts v
             JOIN news_events e ON e.event_id = v.event_id
             WHERE v.stage = 'triage' AND v.event_id = ANY(%s)
             ORDER BY v.event_id, v.created_at_ms DESC
            """,
            (list(event_ids),),
        ).fetchall()
        return [dict(row) for row in rows]

    def append_learning_run_case(
        self,
        *,
        run_sha: str,
        case: Mapping[str, Any],
        dataset_sha: str,
        dataset_role: str,
        stage: str,
        stable_observation: Mapping[str, Any],
        candidate_observation: Mapping[str, Any],
        comparison: Mapping[str, Any],
        now_ms: int,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO news_learning_cases (
              run_sha, case_id, dataset_sha, dataset_role, evaluation_stage, subject_kind, event_id,
              evidence_version, external_snapshot_id, review_id, opened_at_ms,
              evidence_sha256, cluster_id, stratum,
              stable_observation, candidate_observation, comparison, created_at_ms
            ) VALUES (
              %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
              %s::jsonb, %s::jsonb, %s::jsonb, %s
            )
            ON CONFLICT (run_sha, case_id) DO NOTHING
            """,
            (
                run_sha,
                case["case_id"],
                dataset_sha,
                dataset_role,
                stage,
                case["subject_kind"],
                case.get("event_id"),
                case.get("evidence_version"),
                case.get("external_snapshot_id"),
                case.get("review_id"),
                case["opened_at_ms"],
                case["evidence_sha256"],
                case["cluster_id"],
                case["stratum"],
                _dumps(dict(stable_observation)),
                _dumps(dict(candidate_observation)),
                _dumps(dict(comparison)),
                int(now_ms),
            ),
        )

    def learning_run_cases(self, run_sha: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM news_learning_cases WHERE run_sha = %s ORDER BY case_id", (run_sha,)
        ).fetchall()
        return [dict(row) for row in rows]

    def registered_candidate_payloads(self) -> list[dict[str, Any]]:
        """Every registered candidate payload, newest first.

        Newest first because the caller is looking one up by its content hash and a re-registration is
        more likely to be the one being asked about.
        """

        rows = self.conn.execute(
            "SELECT payload FROM news_learning_artifacts WHERE kind = 'candidate' ORDER BY created_at_ms DESC"
        ).fetchall()
        return [dict(row["payload"] or {}) for row in rows]

    def learning_artifacts_of_kind(self, kind: str, artifact_sha: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT artifact_sha, parent_sha, payload FROM news_learning_artifacts "
            "WHERE kind = %s AND artifact_sha = %s",
            (kind, artifact_sha),
        ).fetchall()
        return [dict(row) for row in rows]

    def learning_artifact_read_back(
        self,
        kind: str,
        payload: Mapping[str, Any],
        *,
        parent_sha: str | None,
        created_by: str,
        now_ms: int,
    ) -> str:
        """Append one content-addressed learning artifact and re-read it before it counts.

        Distinct from `_append_learning_artifact` because the release plane needs the read-back: two
        different documents landing on one `artifact_sha` would otherwise make the second silently
        invisible instead of failing the registration a receipt chain later verifies.
        """

        artifact_sha = canonical_sha({"kind": kind, "payload": payload})
        self.conn.execute(
            """
            INSERT INTO news_learning_artifacts (artifact_sha, kind, parent_sha, payload, created_by, created_at_ms)
            VALUES (%s, %s, %s, %s::jsonb, %s, %s) ON CONFLICT (artifact_sha) DO NOTHING
            """,
            (artifact_sha, kind, parent_sha, _dumps(dict(payload)), created_by, int(now_ms)),
        )
        row = self.conn.execute(
            "SELECT kind, payload FROM news_learning_artifacts WHERE artifact_sha = %s", (artifact_sha,)
        ).fetchone()
        if (
            row is None
            or row["kind"] != kind
            or canonical_sha({"kind": kind, "payload": row["payload"]}) != artifact_sha
        ):
            raise ValueError("news_learning_artifact_collision")
        return artifact_sha

    def active_stable_agent_sha(self) -> str | None:
        """The stable bundle the last deployment appointed, or None when no runtime receipt exists.

        Only worker startup/deployment may appoint the active Agent, so the absence is returned rather
        than invented: a reader that needs one raises, and none of them may create one.
        """

        row = self.conn.execute(
            "SELECT payload ->> 'stable_sha' AS stable_sha FROM news_learning_artifacts "
            "WHERE kind = 'active_agent' ORDER BY created_at_ms DESC LIMIT 1"
        ).fetchone()
        return None if row is None else str(row["stable_sha"])

    def reviews_by_id(self, review_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
        if not review_ids:
            return {}
        rows = self.conn.execute("SELECT * FROM news_reviews WHERE review_id = ANY(%s)", (list(review_ids),)).fetchall()
        return {str(row["review_id"]): dict(row) for row in rows}

    def learning_epoch_row_for_bundle(self, bundle_sha: str) -> dict[str, Any] | None:
        """The epoch one exact bundle accrues evidence under, or None until that bundle has deployed.

        Keyed on the bundle rather than on an epoch label (#314): the label is a truncation for humans,
        and two different bundles must never resolve to one epoch because their first eight hex digits
        collide.
        """

        row = self.conn.execute(
            "SELECT epoch_id, starts_at_ms, bundle_sha, envelope_sha256, artifact_schema_version, "
            "baseline_program_version, baseline_program_sha256, prior_evidence_disposition, reset_reason "
            "FROM news_learning_epochs WHERE bundle_sha = %s",
            (bundle_sha,),
        ).fetchone()
        return None if row is None else dict(row)

    def db_now_ms(self) -> int:
        row = self.conn.execute(
            "SELECT floor(extract(epoch FROM clock_timestamp()) * 1000)::bigint AS now_ms"
        ).fetchone()
        return int(row["now_ms"])

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
