"""Historical News rows for integration fixtures only; no production legacy writer."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from tests.support.news_legacy import (
    LEGACY_PROGRAM_VERSION,
    LEGACY_TRIAGE_POLICY_VERSION,
    TriageVerdict,
    legacy_judgment,
)
from tracefold.news.artifact_identity import canonical_sha
from tracefold.news.storage.sql_values import _dumps
from tracefold.news.updates.identity import identity


class LegacyFixtureStorage:
    def __init__(self, news: Any) -> None:
        self.conn = news.conn

    def insert_verdict(
        self,
        *,
        event_id: str,
        stage: str,
        policy_version: str,
        judgment_contract_version: str,
        judgment_origin: str,
        rule_baseline_decision: str,
        final_decision: str,
        override_rule: str | None,
        throttled_by: str | None,
        verdict: Mapping[str, Any],
        verdict_json: str | None = None,
        model_editorial: Mapping[str, Any] | None,
        model_editorial_json: str | None = None,
        judgment_sha256: str,
        runtime_manifest_sha: str,
        model: str | None,
        program_version: str,
        program_sha256: str,
        degraded: bool,
        error_code: str | None,
        trace: Mapping[str, Any],
        trace_json: str | None = None,
        evidence_version: int,
        evidence_sha256: str,
        focus_fact_id: str,
        now_ms: int,
    ) -> bool:
        cursor = self.conn.execute(
            """
            INSERT INTO news_verdicts (
              event_id, stage, policy_version, judgment_contract_version, judgment_origin,
              rule_baseline_decision, final_decision, override_rule,
              throttled_by, verdict, editorial, scored_judgment_sha256, runtime_manifest_sha,
              model, program_version, program_sha256, degraded, error_code, trace, created_at_ms,
              evidence_version, evidence_sha256, focus_fact_id
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s,
                      %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            """,
            (
                event_id,
                stage,
                policy_version,
                judgment_contract_version,
                judgment_origin,
                rule_baseline_decision,
                final_decision,
                override_rule,
                throttled_by,
                verdict_json if verdict_json is not None else _dumps(dict(verdict)),
                (
                    model_editorial_json
                    if model_editorial_json is not None
                    else (_dumps(dict(model_editorial)) if model_editorial is not None else None)
                ),
                judgment_sha256,
                runtime_manifest_sha,
                model,
                program_version,
                program_sha256,
                bool(degraded),
                error_code,
                trace_json if trace_json is not None else _dumps(dict(trace)),
                int(now_ms),
                int(evidence_version),
                evidence_sha256,
                focus_fact_id,
            ),
        )
        return bool(cursor.rowcount)

    def get_verdict(self, *, event_id: str, stage: str, policy_version: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM news_verdicts WHERE event_id = %s AND stage = %s AND policy_version = %s "
            "AND judgment_contract_version IN ('news_judgment_v2', 'news_judgment_v3')",
            (event_id, stage, policy_version),
        ).fetchone()
        return dict(row) if row else None

    def latest_verdict(self, *, event_id: str, stage: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM news_verdicts WHERE event_id = %s AND stage = %s "
            "AND judgment_contract_version IN ('news_judgment_v2', 'news_judgment_v3') "
            "ORDER BY created_at_ms DESC LIMIT 1",
            (event_id, stage),
        ).fetchone()
        return dict(row) if row else None

    def mark_verdict_published(self, *, event_id: str, stage: str, policy_version: str, now_ms: int) -> bool:
        cursor = self.conn.execute(
            """
            UPDATE news_verdicts SET published_at_ms = %s
             WHERE event_id = %s AND stage = %s AND policy_version = %s
               AND judgment_contract_version IN ('news_judgment_v2', 'news_judgment_v3') AND published_at_ms IS NULL
            """,
            (int(now_ms), event_id, stage, policy_version),
        )
        return bool(cursor.rowcount)

    def retire_legacy_delivery_intents(self, *, now_ms: int) -> int:
        """Dead-letter every still-pending legacy intent with an explicit reason; nothing is sent for them."""

        cursor = self.conn.execute(
            """
            UPDATE news_delivery_queue
               SET state = 'dead', error_code = %s, settled_at_ms = %s, updated_at_ms = %s
             WHERE state = 'pending' AND kind IN ('first', 'followup')
            """,
            (LEGACY_INTENT_RETIRED, int(now_ms), int(now_ms)),
        )
        return int(cursor.rowcount or 0)

    def delivery_claim(self, *, event_id: str, kind: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM news_delivery_queue WHERE intent_id = %s",
            (legacy_intent_id(event_id, kind),),
        ).fetchone()
        return dict(row) if row else None

    def begin_delivery(
        self, *, event_id: str, kind: str, card: Mapping[str, Any], now_ms: int, history_context_json: str | None = None
    ) -> str:
        """Returns 'new' when this process owns the send, otherwise the existing state."""

        intent_id = legacy_intent_id(event_id, kind)
        row = self.conn.execute(
            """
            WITH provided AS (SELECT %s::jsonb AS context), supplied AS (
              SELECT CASE WHEN s.event_id IS NULL THEN context ELSE context || jsonb_build_object(
                'comparison_title', s.snapshot #> '{card,comparison_title}',
                'comparison_fingerprint', s.snapshot #> '{card,comparison_fingerprint}',
                'dedupe_family', s.snapshot #> '{card,dedupe_family}') END AS context
              FROM provided LEFT JOIN news_event_evidence_snapshots s
                ON s.event_id=context->>'event_id'
               AND s.evidence_version=(context->>'evidence_version')::integer
            ), bound AS (
              SELECT CASE WHEN context IS NULL THEN NULL ELSE
                context || jsonb_build_object('canonical_assets', COALESCE((
                  SELECT jsonb_agg(symbol ORDER BY symbol) FROM (
                    SELECT DISTINCT COALESCE(a.base_symbol, raw.symbol) AS symbol
                    FROM jsonb_array_elements_text(context->'canonical_assets') raw(symbol)
                    LEFT JOIN news_symbol_aliases a ON a.alias=raw.symbol
                  ) resolved
                ), '[]'::jsonb)) END AS context FROM supplied
            )
            INSERT INTO news_deliveries (
              intent_id, event_id, kind, state, card, attempted_at_ms, created_at_ms, history_context
            )
            SELECT %s, %s, %s, 'sending', %s::jsonb, %s, %s, context FROM bound
            ON CONFLICT (intent_id) DO NOTHING
            RETURNING state
            """,
            (history_context_json, intent_id, event_id, kind, _dumps(dict(card)), int(now_ms), int(now_ms)),
        ).fetchone()
        if row is not None:
            return "new"
        existing = self.conn.execute("SELECT state FROM news_deliveries WHERE intent_id = %s", (intent_id,)).fetchone()
        return str(existing["state"]) if existing else "new"

    def settle_delivery(
        self,
        *,
        event_id: str,
        kind: str,
        state: str,
        receipt: Mapping[str, Any] | None,
        error_code: str | None,
        now_ms: int,
    ) -> bool:
        cursor = self.conn.execute(
            """
            UPDATE news_deliveries SET state = %s, receipt = %s::jsonb, error_code = %s, settled_at_ms = %s
             WHERE intent_id = %s AND state = 'sending'
            """,
            (
                state,
                _dumps(dict(receipt)) if receipt is not None else None,
                error_code,
                int(now_ms),
                legacy_intent_id(event_id, kind),
            ),
        )
        return bool(cursor.rowcount)

    def delivery(self, *, event_id: str, kind: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM news_deliveries WHERE intent_id = %s", (legacy_intent_id(event_id, kind),)
        ).fetchone()
        return dict(row) if row else None


def legacy_news(news: Any) -> LegacyFixtureStorage:
    return LegacyFixtureStorage(news)


def _persist_triage_verdict(
    repos,
    *,
    event_id: str,
    at_ms: int,
    symbol: str,
    direction: str = "bearish",
    headline_zh: str = "阿里巴巴配售新股",
    policy_version: str = LEGACY_TRIAGE_POLICY_VERSION,
    final_decision: str = "push",
    throttled_by: str | None = None,
) -> None:
    from tests.fixtures.news_semantic_0422 import latest_evidence

    evidence = latest_evidence(repos.news.conn, event_id)
    assert evidence is not None
    verdict = TriageVerdict(
        novelty="new_fact",
        assets=[{"symbol": symbol, "role": "primary"}],
        direction=direction,
        scope="single_name",
        fact_kind="state_change",
        evidence_ref="c1",
        confidence=0.9,
        headline_zh=headline_zh,
        why_zh="",
    )
    judgment = legacy_judgment(verdict)
    runtime_manifest_sha = "b" * 64
    trace = {
        "judgment_contract_version": judgment.judgment_contract_version,
        "judgment_origin": "model",
        "judgment_sha256": judgment.scored_judgment_sha256,
        "verdict_sha256": canonical_sha(verdict.model_dump(mode="json")),
        "editorial_sha256": judgment.editorial.editorial_sha256,
        "runtime_manifest_sha": runtime_manifest_sha,
        "program_version": LEGACY_PROGRAM_VERSION,
        "program_sha256": "a" * 64,
        "evidence_version": int(evidence["evidence_version"]),
        "evidence_sha256": str(evidence["evidence_sha256"]),
        "focus_fact_id": str(evidence["focus_fact_id"]),
        "told": [],
        "told_count": 0,
    }
    assert legacy_news(repos.news).insert_verdict(
        event_id=event_id,
        stage="triage",
        policy_version=policy_version,
        judgment_contract_version=judgment.judgment_contract_version,
        judgment_origin="model",
        rule_baseline_decision="push",
        final_decision=final_decision,
        override_rule="fact_kind_state_change",
        throttled_by=throttled_by,
        verdict=verdict.model_dump(mode="json"),
        model_editorial=judgment.editorial.document,
        judgment_sha256=judgment.scored_judgment_sha256,
        runtime_manifest_sha=runtime_manifest_sha,
        model="test",
        program_version=LEGACY_PROGRAM_VERSION,
        program_sha256="a" * 64,
        degraded=False,
        error_code=None,
        trace=trace,
        evidence_version=int(evidence["evidence_version"]),
        evidence_sha256=str(evidence["evidence_sha256"]),
        focus_fact_id=str(evidence["focus_fact_id"]),
        now_ms=at_ms - 1,
    )


LEGACY_INTENT_RETIRED = "legacy_intent_retired"


def legacy_intent_id(event_id: str, kind: str) -> str:
    if kind not in {"first", "followup"}:
        raise ValueError("news_legacy_delivery_kind_invalid")
    return identity("legacy_intent", event_id, kind)
