"""Transactional Trading Analysis ledger for frozen LIVE Cases and paired paper legs."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict
from decimal import Decimal
from typing import Any

from tracefold.trading.engine.forecast import Forecast, PolicyDecision
from tracefold.trading.engine.paper import PaperLeg
from tracefold.trading.engine.target import TargetSelection

ANALYSIS_RUNTIME_SQL = (
    "SELECT runtime_id,heartbeat_at_ms,active_policy,program_sha,model_name,model_configured,"
    "publish_signals,config_digest,fault_code FROM trading_analysis_runtime WHERE runtime_id=%s"
)
ANALYSIS_CASES_SQL = (
    "SELECT c.case_id,c.trigger_kind,c.asset_id,c.native_symbol,c.created_at_ms,c.state,"
    "c.failure_code,c.decided_at_ms,c.geometry_version,c.view_sha256,c.raw_snapshot_ref "
    "FROM trading_cases c WHERE c.created_at_ms>=%s AND (%s::text IS NULL OR c.state=%s) "
    "ORDER BY c.created_at_ms DESC,c.case_id DESC LIMIT %s"
)
ANALYSIS_CASES_FOR_SOURCE_SQL = (
    "SELECT c.case_id,c.trigger_kind,c.asset_id,c.native_symbol,c.created_at_ms,c.state,"
    "c.failure_code,c.decided_at_ms,c.geometry_version,c.view_sha256,c.raw_snapshot_ref "
    "FROM trading_cases c JOIN trading_triggers t USING(trigger_id) "
    "WHERE t.kind='oi' AND t.payload->>'evidence_ref'=%s "
    "AND (%s::text IS NULL OR c.state=%s) "
    "ORDER BY c.created_at_ms DESC,c.case_id DESC LIMIT %s"
)
ANALYSIS_CASE_SQL = (
    "SELECT case_id,trigger_id,trigger_kind,asset_id,native_symbol,mapping_digest,created_at_ms,"
    "root_expires_at_ms,state,claim_token,lease_until_ms,claim_attempt,view,view_sha256,raw_snapshot_ref,"
    "geometry_version,stop_bps,tp_bps,half_spread_bps,reference_price,decided_at_ms,failure_code,"
    "updated_at_ms FROM trading_cases WHERE case_id=%s"
)
ASSESSMENTS_BY_CASE_SQL = (
    "SELECT case_id,program_sha,route,status,forecast,drivers,notes,input_tokens,output_tokens,"
    "started_at_ms,ended_at_ms FROM trading_assessments WHERE case_id=%s ORDER BY program_sha"
)
ACTIONS_BY_CASE_SQL = (
    "SELECT case_id,program_sha,policy_id,policy_version,calibrator_version,action,reason,expected_r,"
    "publish_status,signal_id,decided_at_ms FROM trading_policy_actions WHERE case_id=%s "
    "ORDER BY program_sha,policy_id"
)
PAPER_BY_CASE_SQL = (
    "SELECT case_id,side,geometry_version,status,outcome,reason,anchor_at_ms,exit_at_ms,anchor_price,"
    "exit_price,gross_bps,cost_bps,net_r,labeled_at_ms FROM trading_paper_legs WHERE case_id=%s ORDER BY side"
)


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _sha(value: object) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


class AnalysisStorage:
    conn: Any

    def claim_is_current(self, *, case_id: str, claim_token: str, now_ms: int) -> bool:
        row = self.conn.execute(
            "SELECT claim_token,lease_until_ms FROM trading_cases WHERE case_id=%s FOR UPDATE", (case_id,)
        ).fetchone()
        return row is not None and row["claim_token"] == claim_token and row["lease_until_ms"] > now_ms

    def label_candidates(self, *, now_ms: int, buffer_ms: int, limit: int) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.conn.execute(
                "SELECT c.case_id,c.native_symbol,c.decided_at_ms,c.stop_bps,c.tp_bps,c.half_spread_bps,"
                "c.geometry_version FROM trading_cases c WHERE c.geometry_version IS NOT NULL "
                "AND c.decided_at_ms IS NOT NULL AND c.decided_at_ms + 14400000 + %s <= %s "
                "AND (SELECT count(*) FROM trading_paper_legs l WHERE l.case_id=c.case_id "
                "AND l.geometry_version=c.geometry_version)<2 ORDER BY c.decided_at_ms LIMIT %s",
                (buffer_ms, now_ms, limit),
            ).fetchall()
        ]

    def frozen_cases(self, *, since_ms: int, until_ms: int) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.conn.execute(
                "SELECT case_id,view FROM trading_cases WHERE view IS NOT NULL "
                "AND created_at_ms>=%s AND created_at_ms<%s ORDER BY created_at_ms,case_id",
                (since_ms, until_ms),
            ).fetchall()
        ]

    def recent_asset_context(
        self, *, asset_id: str, known_at_ms: int, exclude_trigger_id: str, limit: int = 5
    ) -> list[dict[str, Any]]:
        """Only facts and amendments visible before this Case's frozen cutoff."""
        if not 1 <= limit <= 10:
            raise ValueError("source_context_limit_invalid")
        facts = self.conn.execute(
            "SELECT trigger_id,source_fact_key,kind,payload,first_visible_at_ms "
            "FROM trading_triggers WHERE selected_asset_id=%s AND trigger_id<>%s "
            "AND first_visible_at_ms<=%s AND first_visible_at_ms>=%s "
            "ORDER BY first_visible_at_ms DESC,trigger_id DESC LIMIT %s",
            (asset_id, exclude_trigger_id, known_at_ms, known_at_ms - 86_400_000, limit),
        ).fetchall()
        if not facts:
            return []
        keys = [row["source_fact_key"] for row in facts]
        amendments = self.conn.execute(
            "SELECT source_fact_key,content_revision,payload,received_at_ms "
            "FROM trading_source_amendments WHERE source_fact_key=ANY(%s) AND received_at_ms<=%s "
            "ORDER BY received_at_ms DESC,update_id DESC",
            (keys, known_at_ms),
        ).fetchall()
        by_key: dict[str, list[dict[str, Any]]] = {}
        for row in amendments:
            by_key.setdefault(row["source_fact_key"], []).append(dict(row))
        return [{**dict(row), "amendments": by_key.get(row["source_fact_key"], [])[:3]} for row in facts]

    def analysis_runtime(self, runtime_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(ANALYSIS_RUNTIME_SQL, (runtime_id,)).fetchone()
        return None if row is None else dict(row)

    def analysis_cases(
        self,
        *,
        since_ms: int,
        limit: int,
        state: str | None = None,
        source_item_id: str | None = None,
    ) -> list[dict[str, Any]]:
        if not 1 <= limit <= 100:
            raise ValueError("analysis_case_limit_invalid")
        sql = ANALYSIS_CASES_FOR_SOURCE_SQL if source_item_id else ANALYSIS_CASES_SQL
        params = (source_item_id, state, state, limit) if source_item_id else (since_ms, state, state, limit)
        return [dict(row) for row in self.conn.execute(sql, params).fetchall()]

    def analysis_case(self, case_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(ANALYSIS_CASE_SQL, (case_id,)).fetchone()
        if row is None:
            return None
        return {
            **dict(row),
            "assessments": [dict(item) for item in self.conn.execute(ASSESSMENTS_BY_CASE_SQL, (case_id,)).fetchall()],
            "policy_actions": [dict(item) for item in self.conn.execute(ACTIONS_BY_CASE_SQL, (case_id,)).fetchall()],
            "paper_legs": [dict(item) for item in self.conn.execute(PAPER_BY_CASE_SQL, (case_id,)).fetchall()],
        }

    def accept_trigger(
        self,
        *,
        kind: str,
        source_fact_key: str,
        source_revision: str,
        payload_sha256: str,
        payload: dict[str, Any],
        selection: TargetSelection,
        now_ms: int,
        root_ttl_ms: int,
    ) -> tuple[str, str | None, str]:
        """Deduplicate the public News fact; only a selected target gets a Case."""
        if kind not in ("oi", "catalyst") or root_ttl_ms <= 0:
            raise ValueError("analysis_trigger_invalid")
        trigger_id = _sha((kind, source_fact_key, source_revision))
        case_id = _sha((trigger_id, "case_v1")) if selection.reason == "selected" else None
        observed = int(payload.get("first_available_at_ms") or payload.get("provider_event_at_ms") or now_ms)
        target = {
            "reason": selection.reason,
            "asset_id": None if selection.asset_id is None else selection.asset_id.key,
            "instrument": None
            if selection.instrument is None
            else {
                "native_symbol": selection.instrument.native_symbol,
                "mapping_semantics_digest": selection.instrument.semantics_digest,
                "asset_id": selection.instrument.asset_id.key,
                "units_per_contract": str(selection.instrument.units_per_contract),
            },
            "candidates": selection.candidates,
            "registry_snapshot_ref": selection.registry_snapshot_ref,
            "version": selection.version,
        }
        self.conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,746))", (f"source|{source_fact_key}",))
        self.conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,746))",
            (f"{kind}|{source_fact_key}|{source_revision}",),
        )
        existing = self.conn.execute(
            "SELECT trigger_id,payload_sha256 FROM trading_triggers "
            "WHERE kind=%s AND source_fact_key=%s AND source_revision=%s FOR UPDATE",
            (kind, source_fact_key, source_revision),
        ).fetchone()
        if existing is not None:
            if existing["payload_sha256"] == payload_sha256:
                return trigger_id, case_id, "duplicate"
            return trigger_id, case_id, "source_conflict"
        self.conn.execute(
            "INSERT INTO trading_triggers (trigger_id,kind,source_fact_key,source_revision,payload_sha256,"
            "payload,first_visible_at_ms,source_observed_at_ms,selected_asset_id,target_selection,"
            "exclusion_reason,created_at_ms) VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s::jsonb,%s,%s)",
            (
                trigger_id,
                kind,
                source_fact_key,
                source_revision,
                payload_sha256,
                _json(payload),
                observed,
                observed,
                None if selection.asset_id is None or case_id is None else selection.asset_id.key,
                _json(target),
                None if case_id is not None else selection.reason,
                now_ms,
            ),
        )
        if case_id is not None:
            if selection.asset_id is None or selection.instrument is None:
                raise ValueError("selected_trigger_target_missing")
            self.conn.execute(
                "INSERT INTO trading_cases (case_id,trigger_id,trigger_kind,asset_id,native_symbol,"
                "mapping_digest,created_at_ms,root_expires_at_ms,state,updated_at_ms) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'pending',%s)",
                (
                    case_id,
                    trigger_id,
                    kind,
                    selection.asset_id.key,
                    selection.instrument.native_symbol,
                    selection.instrument.semantics_digest,
                    now_ms,
                    now_ms + root_ttl_ms,
                    now_ms,
                ),
            )
        return trigger_id, case_id, "accepted"

    def receive_source_update(
        self,
        *,
        update_id: str,
        source_fact_key: str,
        content_revision: str,
        affected_claim_refs: tuple[str, ...],
        retired_claim_refs: tuple[str, ...],
        payload: dict[str, Any],
        payload_sha256: str,
        now_ms: int,
    ) -> str:
        if not affected_claim_refs or not set(retired_claim_refs) <= set(affected_claim_refs):
            raise ValueError("source_amendment_claims_invalid")
        self.conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,746))", (f"source|{source_fact_key}",))
        inserted = self.conn.execute(
            "INSERT INTO trading_source_amendments (update_id,source_fact_key,content_revision,"
            "affected_claim_refs,retired_claim_refs,payload,payload_sha256,received_at_ms) "
            "VALUES (%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s,%s) "
            "ON CONFLICT (update_id) DO NOTHING RETURNING update_id",
            (
                update_id,
                source_fact_key,
                content_revision,
                _json(sorted(set(affected_claim_refs))),
                _json(sorted(set(retired_claim_refs))),
                _json(payload),
                payload_sha256,
                now_ms,
            ),
        ).fetchone()
        if inserted is not None:
            return "accepted"
        original = self.conn.execute(
            "SELECT payload_sha256 FROM trading_source_amendments WHERE update_id=%s", (update_id,)
        ).fetchone()
        if original is None:
            raise RuntimeError("source_amendment_conflict_missing")
        if original["payload_sha256"] == payload_sha256:
            return "duplicate"
        return "source_conflict"

    def claim_case(self, *, now_ms: int, lease_ms: int) -> dict[str, Any] | None:
        if lease_ms <= 0:
            raise ValueError("analysis_lease_invalid")
        self.conn.execute(
            "UPDATE trading_cases SET state='pending',claim_token=NULL,lease_until_ms=NULL,updated_at_ms=%s "
            "WHERE state='running' AND lease_until_ms<%s",
            (now_ms, now_ms),
        )
        # A stale claimant can write only while its token and lease still match.
        row = self.conn.execute(
            "SELECT * FROM trading_cases c WHERE (state='pending' OR "
            "(state='running' AND lease_until_ms<%s)) "
            "AND NOT EXISTS (SELECT 1 FROM trading_cases other WHERE other.asset_id=c.asset_id "
            "AND other.case_id<>c.case_id AND other.state='running' AND other.lease_until_ms>=%s) "
            "ORDER BY created_at_ms,case_id FOR UPDATE SKIP LOCKED LIMIT 1",
            (now_ms, now_ms),
        ).fetchone()
        if row is None:
            return None
        self.conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,746))", (f"case-asset|{row['asset_id']}",))
        active = self.conn.execute(
            "SELECT 1 FROM trading_cases WHERE asset_id=%s AND case_id<>%s "
            "AND state='running' AND lease_until_ms>=%s LIMIT 1",
            (row["asset_id"], row["case_id"], now_ms),
        ).fetchone()
        if active is not None:
            return None
        token = uuid.uuid4().hex
        self.conn.execute(
            "UPDATE trading_cases SET state='running',claim_token=%s,lease_until_ms=%s,"
            "claim_attempt=claim_attempt+1,updated_at_ms=%s WHERE case_id=%s",
            (token, now_ms + lease_ms, now_ms, row["case_id"]),
        )
        return {**dict(row), "state": "running", "claim_token": token, "lease_until_ms": now_ms + lease_ms}

    def case_trigger(self, case_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT t.* FROM trading_cases c JOIN trading_triggers t USING(trigger_id) WHERE c.case_id=%s",
            (case_id,),
        ).fetchone()
        return None if row is None else dict(row)

    def publication_source_status(self, *, case_id: str, now_ms: int) -> str | None:
        """Check News correction and later public facts at the final publish fence."""
        identity = self.conn.execute(
            "SELECT t.source_fact_key FROM trading_cases c JOIN trading_triggers t USING(trigger_id) "
            "WHERE c.case_id=%s",
            (case_id,),
        ).fetchone()
        if identity is None:
            raise ValueError("analysis_trigger_missing")
        self.conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,746))",
            (f"source|{identity['source_fact_key']}",),
        )
        original = self.conn.execute(
            "SELECT t.* FROM trading_cases c JOIN trading_triggers t USING(trigger_id) "
            "WHERE c.case_id=%s FOR SHARE OF c,t",
            (case_id,),
        ).fetchone()
        if original is None:
            raise ValueError("analysis_trigger_missing")
        if original["kind"] == "catalyst":
            claims = list(original["payload"].get("claim_refs") or ())
            if claims:
                correction = self.conn.execute(
                    "SELECT 1 FROM trading_source_amendments WHERE source_fact_key=%s "
                    "AND received_at_ms<=%s AND retired_claim_refs ?| %s LIMIT 1",
                    (original["source_fact_key"], now_ms, claims),
                ).fetchone()
                if correction is not None:
                    return "source_corrected"
                superseded = self.conn.execute(
                    "SELECT 1 FROM trading_triggers WHERE kind='catalyst' AND trigger_id<>%s "
                    "AND created_at_ms<=%s AND payload->'superseded_claim_refs' ?| %s LIMIT 1",
                    (original["trigger_id"], now_ms, claims),
                ).fetchone()
                if superseded is not None:
                    return "source_superseded"
        else:
            superseded = self.conn.execute(
                "SELECT 1 FROM trading_triggers WHERE kind='oi' AND source_fact_key=%s "
                "AND source_revision<>%s AND source_observed_at_ms>%s AND created_at_ms<=%s LIMIT 1",
                (original["source_fact_key"], original["source_revision"], original["source_observed_at_ms"], now_ms),
            ).fetchone()
            if superseded is not None:
                return "source_superseded"
        return None

    def freeze_case(
        self,
        *,
        case_id: str,
        claim_token: str,
        now_ms: int,
        view: dict[str, Any],
        raw_snapshot_ref: str,
        geometry_version: str,
        stop_bps: int,
        tp_bps: int,
        half_spread_bps: Decimal,
        reference_price: Decimal,
    ) -> bool:
        digest = _sha(view)
        row = self.conn.execute(
            "UPDATE trading_cases SET view=COALESCE(view,%s::jsonb),"
            "view_sha256=COALESCE(view_sha256,%s),raw_snapshot_ref=COALESCE(raw_snapshot_ref,%s),"
            "geometry_version=COALESCE(geometry_version,%s),stop_bps=COALESCE(stop_bps,%s),"
            "tp_bps=COALESCE(tp_bps,%s),half_spread_bps=COALESCE(half_spread_bps,%s),"
            "reference_price=COALESCE(reference_price,%s),updated_at_ms=%s "
            "WHERE case_id=%s AND state='running' AND claim_token=%s AND lease_until_ms>%s "
            "AND (view_sha256 IS NULL OR view_sha256=%s) RETURNING case_id",
            (
                _json(view),
                digest,
                raw_snapshot_ref,
                geometry_version,
                stop_bps,
                tp_bps,
                half_spread_bps,
                reference_price,
                now_ms,
                case_id,
                claim_token,
                now_ms,
                digest,
            ),
        ).fetchone()
        return row is not None

    def pit_base_rates(
        self, *, trigger_kind: str, known_at_ms: int
    ) -> dict[str, tuple[int, dict[str, Decimal] | None]]:
        rows = self.conn.execute(
            "SELECT l.side,l.outcome,count(*) AS n FROM trading_paper_legs l "
            "JOIN trading_cases c USING(case_id) WHERE c.trigger_kind=%s "
            "AND l.status='complete' AND l.labeled_at_ms<%s AND l.exit_at_ms<%s "
            "AND c.created_at_ms>=%s GROUP BY l.side,l.outcome",
            (trigger_kind, known_at_ms, known_at_ms, known_at_ms - 14 * 86_400_000),
        ).fetchall()
        result: dict[str, tuple[int, dict[str, Decimal] | None]] = {}
        for side in ("long", "short"):
            counts = {str(row["outcome"]): int(row["n"]) for row in rows if row["side"] == side}
            total = sum(counts.values())
            result[side] = (
                total,
                None
                if total < 30
                else {outcome: Decimal(counts.get(outcome, 0)) / total for outcome in ("tp", "sl", "timeout")},
            )
        return result

    def record_assessment(
        self,
        *,
        case_id: str,
        program_sha: str,
        route: str,
        status: str,
        forecast: Forecast | None,
        notes: tuple[str, ...],
        usage: dict[str, Any],
        started_at_ms: int,
        ended_at_ms: int,
    ) -> None:
        value = (
            None
            if forecast is None
            else {
                side: {name: str(getattr(getattr(forecast, side), name)) for name in ("p_tp", "p_sl", "p_timeout")}
                for side in ("long", "short")
            }
        )
        drivers = [] if forecast is None else [asdict(item) for item in forecast.drivers]
        inserted = self.conn.execute(
            "INSERT INTO trading_assessments (case_id,program_sha,route,status,forecast,drivers,notes,"
            "input_tokens,output_tokens,started_at_ms,ended_at_ms) "
            "VALUES (%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s,%s,%s,%s) "
            "ON CONFLICT(case_id,program_sha) DO NOTHING RETURNING case_id",
            (
                case_id,
                program_sha,
                route,
                status,
                None if value is None else _json(value),
                _json(drivers),
                _json(notes),
                usage.get("input_tokens"),
                usage.get("output_tokens"),
                started_at_ms,
                ended_at_ms,
            ),
        ).fetchone()
        if inserted is None:
            prior = self.conn.execute(
                "SELECT route,status,forecast FROM trading_assessments WHERE case_id=%s AND program_sha=%s",
                (case_id, program_sha),
            ).fetchone()
            if prior is None or (prior["route"], prior["status"], prior["forecast"]) != (route, status, value):
                raise ValueError("assessment_identity_conflict")

    def record_policy_actions(
        self, *, case_id: str, program_sha: str, decisions: tuple[PolicyDecision, ...], now_ms: int
    ) -> None:
        for item in decisions:
            self.conn.execute(
                "INSERT INTO trading_policy_actions (case_id,program_sha,policy_id,policy_version,"
                "calibrator_version,action,reason,expected_r,publish_status,decided_at_ms) "
                "VALUES (%s,%s,%s,%s,'identity_v1',%s,%s,%s,%s,%s) "
                "ON CONFLICT(case_id,program_sha,policy_id,policy_version) DO NOTHING",
                (
                    case_id,
                    program_sha,
                    item.policy_id,
                    item.version,
                    item.action,
                    item.reason,
                    item.expected_r,
                    "abstained" if item.action == "abstain" else "not_live",
                    now_ms,
                ),
            )

    def set_publication(
        self,
        *,
        case_id: str,
        program_sha: str,
        policy_id: str,
        policy_version: str,
        publish_status: str,
        signal_id: str | None,
    ) -> None:
        row = self.conn.execute(
            "UPDATE trading_policy_actions SET publish_status=%s,signal_id=%s "
            "WHERE case_id=%s AND program_sha=%s AND policy_id=%s AND policy_version=%s "
            "RETURNING case_id",
            (publish_status, signal_id, case_id, program_sha, policy_id, policy_version),
        ).fetchone()
        if row is None:
            raise ValueError("publication_action_missing")

    def record_paper_legs(
        self, *, case_id: str, legs: tuple[PaperLeg, PaperLeg], geometry_version: str, now_ms: int
    ) -> None:
        for leg in legs:
            self.conn.execute(
                "INSERT INTO trading_paper_legs (case_id,side,geometry_version,status,outcome,reason,anchor_at_ms,"
                "exit_at_ms,anchor_price,exit_price,gross_bps,cost_bps,net_r,labeled_at_ms) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT(case_id,side,geometry_version) DO NOTHING",
                (
                    case_id,
                    leg.side,
                    geometry_version,
                    leg.status,
                    leg.outcome,
                    leg.reason,
                    leg.anchor_at_ms,
                    leg.exit_at_ms,
                    leg.anchor_price,
                    leg.exit_price,
                    leg.gross_bps,
                    leg.cost_bps,
                    leg.net_r,
                    now_ms,
                ),
            )

    def finish_case(
        self, *, case_id: str, claim_token: str, status: str, failure_code: str | None, now_ms: int
    ) -> bool:
        row = self.conn.execute(
            "UPDATE trading_cases SET state=%s,claim_token=NULL,lease_until_ms=NULL,"
            "failure_code=%s,decided_at_ms=%s,updated_at_ms=%s "
            "WHERE case_id=%s AND state='running' AND claim_token=%s AND lease_until_ms>%s RETURNING case_id",
            (status, failure_code, now_ms, now_ms, case_id, claim_token, now_ms),
        ).fetchone()
        return row is not None

    def heartbeat_analysis_runtime(
        self,
        *,
        runtime_id: str,
        now_ms: int,
        active_policy: str,
        program_sha: str,
        model_name: str | None,
        model_configured: bool,
        publish_signals: bool,
        config_digest: str,
        fault_code: str | None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO trading_analysis_runtime (runtime_id,heartbeat_at_ms,active_policy,program_sha,"
            "model_name,model_configured,publish_signals,config_digest,fault_code) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(runtime_id) DO UPDATE SET "
            "heartbeat_at_ms=EXCLUDED.heartbeat_at_ms,active_policy=EXCLUDED.active_policy,"
            "program_sha=EXCLUDED.program_sha,model_name=EXCLUDED.model_name,"
            "model_configured=EXCLUDED.model_configured,publish_signals=EXCLUDED.publish_signals,"
            "config_digest=EXCLUDED.config_digest,fault_code=EXCLUDED.fault_code",
            (
                runtime_id,
                now_ms,
                active_policy,
                program_sha,
                model_name,
                model_configured,
                publish_signals,
                config_digest,
                fault_code,
            ),
        )
