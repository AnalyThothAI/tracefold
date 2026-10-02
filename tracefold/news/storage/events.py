"""Material News Items, Events, memberships, outbox state, and evidence snapshots."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from types import SimpleNamespace
from typing import Any, cast

# S608 exemptions below interpolate only code-owned limits/admission literals; provider values stay bound.
from ..models import ADMITTED_ADMISSIONS
from ..opennews import source_artifact_identity
from ..source_contracts import EventKind
from .feed_sql import CURRENT_EVENT_CARD_SQL, EDITORIAL_EVENT_CARD_SQL
from .sql_values import _dumps
from .update_commit import lock_event

BAND_CANDIDATES_SQL = """
WITH hits AS MATERIALIZED (SELECT event_id FROM news_events WHERE dedupe_bands && %s::text[])
SELECT e.event_id,e.comparison_title,e.leader_title,e.opened_at_ms,e.grounded_assets
  FROM hits JOIN news_events e USING(event_id)
 WHERE e.dedupe_family=%s AND e.expires_at_ms>%s AND e.event_kind=%s AND e.admission=ANY(%s)
   AND e.evidence_version IS NOT NULL ORDER BY e.opened_at_ms LIMIT 25
"""


def joinable_admissions(admission: str) -> list[str]:
    """The admissions of Events a new frame may join by exact text or near match.

    Timely live or recovered evidence joins only admitted Events. History-only evidence may also join
    a recovery Event, without requesting semantic work.
    """

    admitted = sorted(ADMITTED_ADMISSIONS)
    return [*admitted, "recovery"] if admission == "recovery" else admitted


def prepare_evidence_snapshot(
    material: Mapping[str, Any],
    *,
    event_id: str,
    now_ms: int,
    focus_fact: Any | None,
) -> dict[str, Any]:
    """Build and hash an evidence snapshot with no database transaction open."""

    card = dict(material["card"])
    members = list(material["members"])
    latest_value = material.get("latest")
    latest = dict(latest_value) if isinstance(latest_value, Mapping) else None
    previous = dict(latest["snapshot"] or {}) if latest is not None else {}
    focus_item_id = material.get("focus_item_id")
    if (focus_item_id is None) != (focus_fact is None):
        raise ValueError("news_event_evidence_focus_incomplete")
    if focus_fact is not None:
        focus = {
            "fact_id": str(focus_fact.fact_id),
            "text": str(focus_fact.text),
            "context": str(focus_fact.context),
            "method": str(focus_fact.method),
            "span_start": int(focus_fact.span_start),
            "span_end": int(focus_fact.span_end),
        }
        focus_source = dict(material["focus_source"])
    elif previous:
        focus = dict(previous.get("focus_fact") or {})
        focus_source = dict(previous.get("card") or {})
    else:
        focus = {
            "fact_id": str(card.get("focus_fact_id") or ""),
            "text": str(card.get("focus_fact_text") or card.get("leader_title") or ""),
            "context": str(card.get("focus_fact_context") or ""),
            "method": str(card.get("focus_fact_method") or "whole_item"),
            "span_start": int(card.get("focus_span_start") or 0),
            "span_end": int(card.get("focus_span_end") or 0),
        }
        focus_source = card
    snapshot_card = {
        key: card.get(key)
        for key in (
            "event_id",
            "leader_item_id",
            "dedupe_family",
            "event_kind",
            "comparison_fingerprint",
            "comparison_title",
            "opened_at_ms",
            "last_member_at_ms",
            "expires_at_ms",
            "member_count",
            "admission",
            "queue_priority",
            "provider_score_max",
            "engine_type",
            "asset_class",
            "grounded_assets",
            "watchlist_hits",
            "macro_lexicon",
            "storyline_key",
            "ingest_mode",
            "trace_id",
            "leader_url",
            "reporting_origin",
            "provider_metadata",
            "provenance",
            "leader_published_at_ms",
            "raw_first_line",
        )
    }
    snapshot_card.update(
        {
            key: focus_source.get(key)
            for key in (
                "leader_item_id",
                "leader_url",
                "reporting_origin",
                "provider_metadata",
                "provenance",
                "leader_published_at_ms",
                "raw_first_line",
            )
        }
    )
    snapshot_card.update(
        {
            "leader_title": focus["text"],
            "leader_description": focus["context"],
            "focus_fact_id": focus["fact_id"],
        }
    )
    if focus.get("method") == "explicit_numbered":
        snapshot_card["raw_first_line"] = ""
    _, artifact_published_at_ms = source_artifact_identity(str(snapshot_card.get("leader_url") or ""))
    pushed_at_ms = snapshot_card.get("leader_published_at_ms")
    if artifact_published_at_ms is not None and pushed_at_ms:
        snapshot_card["source_age_s"] = max(0, (int(pushed_at_ms) - artifact_published_at_ms) // 1000)
    snapshot = {
        "schema_version": "news_event_evidence_v3",
        "event_id": event_id,
        "focus_fact": focus,
        "card": snapshot_card,
        "members": [_snapshot_member(row) for row in members],
        "provenance": "observed",
    }
    serialized = _dumps(snapshot)
    evidence_sha = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    material_sha = hashlib.sha256(_dumps(semantic_material(snapshot)).encode()).hexdigest()
    previous_version = None if latest is None else int(latest["evidence_version"])
    return {
        "event_id": event_id,
        "previous_version": previous_version,
        "previous_sha256": None if latest is None else str(latest["evidence_sha256"]),
        "evidence_version": 1 if previous_version is None else previous_version + 1,
        "focus_fact_id": str(focus["fact_id"]),
        "evidence_sha256": evidence_sha,
        "material_sha256": material_sha,
        "semantic_changed": latest is None or latest.get("material_sha256") != material_sha,
        "snapshot_json": serialized,
        "now_ms": int(now_ms),
    }


def semantic_material(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """What a semantic turn reads from an evidence snapshot.

    The task scope, the members' records, facts and body revisions, and the grounded assets. A strategy or
    provenance merged into a member, a score or a queue priority is recorded in the snapshot but is not new
    material: a snapshot that changes only these needs no semantic work.
    """

    card = snapshot.get("card") or {}
    return {
        "focus_fact": snapshot.get("focus_fact"),
        "leader_item_id": card.get("leader_item_id"),
        "grounded_assets": card.get("grounded_assets"),
        "members": [
            {key: member.get(key) for key in ("item_id", "fact_id", "fact_text", "evidence_revisions")}
            for member in snapshot.get("members") or ()
        ],
    }


def _snapshot_member(row: Mapping[str, Any]) -> dict[str, Any]:
    """One frozen member. Later body revisions are named only when the Item has some, so a member
    without one keeps the exact snapshot bytes -- and digest -- it always had."""

    member: dict[str, Any] = {
        "item_id": str(row["item_id"]),
        "fact_id": str(row["fact_id"]),
        "fact_text": str(row["fact_text"]),
        "joined_at_ms": int(row["joined_at_ms"]),
        "match_kind": str(row["match_kind"]),
        "jaccard_estimate": row["jaccard_estimate"],
        "reporting_origin": str(row["reporting_origin"] or ""),
        "canonical_url": row["canonical_url"],
        "provider_metadata": dict(row["provider_metadata"] or {}),
        "provenance": list(row["provenance"] or []),
    }
    revisions = [str(value) for value in row.get("evidence_revisions") or ()]
    if revisions:
        member["evidence_revisions"] = revisions
    return member


class EventStorage:
    conn: Any

    def upsert_item(
        self,
        *,
        item_id: str,
        source_id: str,
        source_item_key: str,
        title: str,
        raw_first_line: str,
        description: str,
        canonical_url: str | None,
        reporting_origin: str,
        published_at_ms: int,
        observed_at_ms: int,
        provider_metadata_json: str,
        strategy_ids_json: str,
        ingest_mode: str,
        trace_id: str,
        now_ms: int,
        source_artifact_id: str = "",
        provider_params_json: str = "{}",
        provider_params_sha256: str | None = None,
        evidence_text: str | None = None,
        evidence_text_sha256: str | None = None,
    ) -> bool:
        """Insert or merge provenance. Returns True when the Item is new.

        A replay merges source Strategies and provenance while retaining the first body.
        Later, different bodies are recorded by `record_item_revision` beside the original.
        """

        row = self.conn.execute(
            """
            INSERT INTO news_items (
              item_id, source_id, source_item_key, title, raw_first_line, description, canonical_url,
              reporting_origin, published_at_ms, observed_at_ms, provider_metadata, provenance,
              first_ingest_mode, trace_id, created_at_ms, updated_at_ms, source_artifact_id,
              provider_params, provider_params_available_at_ms, provider_params_sha256,
              evidence_text, evidence_text_sha256
            ) VALUES (
              %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s, %s, %s,
              %s::jsonb, %s, %s, %s, %s
            )
            ON CONFLICT (item_id) DO UPDATE SET
              provider_metadata = jsonb_set(
                news_items.provider_metadata,
                '{strategies}',
                (
                  SELECT COALESCE(
                    jsonb_agg(value ORDER BY source_rank, existing_ordinal NULLS LAST, value),
                    '[]'::jsonb
                  )
                    FROM (
                      SELECT value, min(source_rank) AS source_rank,
                             min(original_ordinal) FILTER (WHERE source_rank = 0) AS existing_ordinal
                        FROM (
                          SELECT value, 0::smallint AS source_rank, ordinality::bigint AS original_ordinal
                            FROM jsonb_array_elements(
                              COALESCE(news_items.provider_metadata -> 'strategies', '[]'::jsonb)
                            ) WITH ORDINALITY AS existing(value, ordinality)
                          UNION ALL
                          SELECT value, 1::smallint, NULL::bigint
                            FROM jsonb_array_elements(
                              COALESCE(EXCLUDED.provider_metadata -> 'strategies', '[]'::jsonb)
                            ) AS incoming(value)
                        ) combined
                       GROUP BY value
                    ) deduplicated
                ),
                true
              ),
              provenance = (
                SELECT COALESCE(jsonb_agg(DISTINCT value ORDER BY value), '[]'::jsonb)
                  FROM jsonb_array_elements_text(news_items.provenance || EXCLUDED.provenance) AS t(value)
              ),
              provider_params = CASE
                WHEN news_items.provider_params = '{}'::jsonb THEN EXCLUDED.provider_params
                ELSE news_items.provider_params END,
              provider_params_available_at_ms = CASE WHEN news_items.provider_params = '{}'::jsonb
                THEN EXCLUDED.provider_params_available_at_ms ELSE news_items.provider_params_available_at_ms END,
              provider_params_sha256 = CASE WHEN news_items.provider_params = '{}'::jsonb
                THEN EXCLUDED.provider_params_sha256 ELSE news_items.provider_params_sha256 END,
              evidence_text = CASE WHEN news_items.provider_params = '{}'::jsonb
                THEN EXCLUDED.evidence_text ELSE news_items.evidence_text END,
              evidence_text_sha256 = CASE WHEN news_items.provider_params = '{}'::jsonb
                THEN EXCLUDED.evidence_text_sha256 ELSE news_items.evidence_text_sha256 END,
              updated_at_ms = GREATEST(news_items.updated_at_ms, EXCLUDED.updated_at_ms)
            RETURNING (xmax = 0) AS inserted
            """,
            (
                item_id,
                source_id,
                source_item_key,
                title,
                raw_first_line,
                description,
                canonical_url,
                reporting_origin,
                int(published_at_ms),
                int(observed_at_ms),
                provider_metadata_json,
                strategy_ids_json,
                ingest_mode,
                trace_id,
                int(now_ms),
                int(now_ms),
                source_artifact_id,
                provider_params_json,
                int(now_ms) if provider_params_sha256 is not None else None,
                provider_params_sha256,
                evidence_text,
                evidence_text_sha256,
            ),
        ).fetchone()
        return bool(row["inserted"])

    def record_item_revision(
        self,
        *,
        item_id: str,
        evidence_text: str,
        evidence_text_sha256: str,
        provider_params_json: str,
        reporting_origin: str,
        canonical_url: str | None,
        source_artifact_id: str,
        published_at_ms: int,
        received_at_ms: int,
    ) -> bool:
        """Keep a changed body or source attribution as a later evidence revision.

        The first evidence stays on `news_items`; identical current content writes nothing.
        A later return to older content has its own predecessor and evidence identity.
        """

        if not evidence_text.strip():
            return False
        content_sha256 = hashlib.sha256(
            _dumps((evidence_text_sha256, reporting_origin, canonical_url, source_artifact_id)).encode("utf-8")
        ).hexdigest()
        item = self.conn.execute(
            "SELECT evidence_text_sha256, reporting_origin, canonical_url, source_artifact_id, "
            "provider_params, observed_at_ms, evidence_observed_at_ms,revisions "
            "FROM news_items WHERE item_id=%s FOR UPDATE",
            (item_id,),
        ).fetchone()
        if item is None or not item["provider_params"]:
            return False
        revisions = list(item["revisions"])
        latest = revisions[-1] if revisions else None
        original_sha = hashlib.sha256(
            _dumps(
                (
                    item["evidence_text_sha256"],
                    item["reporting_origin"],
                    item["canonical_url"],
                    item["source_artifact_id"],
                )
            ).encode("utf-8")
        ).hexdigest()
        current_sha = original_sha if latest is None else str(latest["content_sha256"])
        highwater = int(item["evidence_observed_at_ms"] or item["observed_at_ms"])
        # The receiver's immutable envelope clock orders local observations. Publication time is
        # not an edit version. A new connection observation may legitimately return to an old body.
        if received_at_ms < highwater:
            return False
        self.conn.execute(
            "UPDATE news_items SET evidence_observed_at_ms=%s WHERE item_id=%s",
            (int(received_at_ms), item_id),
        )
        if current_sha == content_sha256:
            return False
        if received_at_ms == int(item["observed_at_ms"]) and content_sha256 == original_sha:
            return False
        if any(r["received_at_ms"] == received_at_ms and r["content_sha256"] == content_sha256 for r in revisions):
            return False
        previous = original_sha if latest is None else str(latest["revision_sha256"])
        revision_sha256 = hashlib.sha256(_dumps((previous, content_sha256, int(received_at_ms))).encode()).hexdigest()
        revisions.append(
            {
                "revision_sha256": revision_sha256,
                "content_sha256": content_sha256,
                "previous_revision_sha256": previous,
                "revision_sequence": len(revisions) + 1,
                "evidence_text": evidence_text,
                "provider_params": __import__("json").loads(provider_params_json),
                "reporting_origin": reporting_origin,
                "canonical_url": canonical_url,
                "source_artifact_id": source_artifact_id,
                "published_at_ms": int(published_at_ms),
                "received_at_ms": int(received_at_ms),
            }
        )
        self.conn.execute("UPDATE news_items SET revisions=%s::jsonb WHERE item_id=%s", (_dumps(revisions), item_id))
        return True

    def item_event_ids(self, item_id: str) -> list[str]:
        """Every Event this Item is evidence of: a revised body is new evidence for each of them."""

        rows = self.conn.execute(
            "SELECT DISTINCT event_id FROM news_event_members WHERE item_id = %s ORDER BY event_id",
            (item_id,),
        ).fetchall()
        return [str(row["event_id"]) for row in rows]

    def find_artifact_event(
        self,
        *,
        source_artifact_id: str,
        dedupe_family: str,
        event_kind: EventKind,
        fingerprint: str,
        item_id: str,
        opened_after_ms: int,
    ) -> dict[str, Any] | None:
        """The Event another Item built from this same source artifact and this same fact (#154).

        The fingerprint is part of the key, not decoration. Without it a digest split into four FactUnits would
        collapse into one Event the second time the provider sent it, because all four units share the artifact.
        With it, unit *k* can only join unit *k*.

        What the artifact id buys is the right to ignore the two guards the text-derived path needs: the
        three-token `shareable` floor (a tweet titled `What a coincidence!` scores below it and so was never
        looked up at all — the provider sent it twice, four seconds apart, under two URL spellings, and the
        reader got two cards) and the 12 h dedupe-family window (`opened_after_ms` is the caller's longer horizon).
        Both guards exist because *text* similarity is evidence; artifact identity is not evidence, it is the
        platform's own primary key.
        """

        if not source_artifact_id:
            return None
        # Only an admitted Event carrying the v3 evidence contract may absorb a live frame. That exact
        # contract boundary prevents a post-cut Item from disappearing into pre-cut evidence without shortening
        # #154's deliberate seven-day artifact window back to the ordinary 12 h dedupe-family window. Recovery and
        # suppressed Events are excluded by admission because neither can produce the live reader card this Item
        # represents.
        row = self.conn.execute(
            """
            SELECT e.event_id, e.opened_at_ms, e.expires_at_ms, e.admission, e.published_at_ms
              FROM news_items i
              JOIN news_event_members m ON m.item_id = i.item_id
              JOIN news_events e ON e.event_id = m.event_id
             WHERE i.source_artifact_id = %s AND i.item_id <> %s
               AND e.dedupe_family = %s AND e.event_kind = %s AND e.comparison_fingerprint = %s
               AND e.opened_at_ms >= %s
               AND e.evidence_version IS NOT NULL
               AND e.admission = ANY(%s)
             ORDER BY e.opened_at_ms ASC LIMIT 1
            """,
            (
                source_artifact_id,
                item_id,
                dedupe_family,
                event_kind,
                fingerprint,
                int(opened_after_ms),
                sorted(ADMITTED_ADMISSIONS),
            ),
        ).fetchone()
        return dict(row) if row else None

    def find_exact_event(
        self,
        *,
        dedupe_family: str,
        event_kind: EventKind,
        fingerprint: str,
        now_ms: int,
        admission: str,
    ) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT e.event_id, e.opened_at_ms, e.expires_at_ms, e.admission, e.published_at_ms
              FROM news_events e
             WHERE e.dedupe_family = %s AND e.event_kind = %s AND e.admission = ANY(%s)
               AND e.comparison_fingerprint = %s AND e.expires_at_ms > %s
               AND e.evidence_version IS NOT NULL
             ORDER BY opened_at_ms ASC LIMIT 1
            """,
            (dedupe_family, event_kind, joinable_admissions(admission), fingerprint, int(now_ms)),
        ).fetchone()
        return dict(row) if row else None

    def find_band_candidates(
        self,
        *,
        dedupe_family: str,
        event_kind: EventKind,
        band_keys: Sequence[str],
        now_ms: int,
        admission: str,
    ) -> list[dict[str, Any]]:
        pairs = [(index, key) for index, key in enumerate(band_keys)]
        if not pairs:
            return []
        rows = self.conn.execute(
            BAND_CANDIDATES_SQL,
            (
                [f"{index}:{key}" for index, key in pairs],
                dedupe_family,
                int(now_ms),
                event_kind,
                joinable_admissions(admission),
            ),
        ).fetchall()
        return [dict(r) for r in rows]

    def insert_event(
        self,
        *,
        event_id: str,
        leader_item_id: str,
        dedupe_family: str,
        event_kind: EventKind,
        comparison_fingerprint: str,
        comparison_title: str,
        leader_title: str,
        focus_fact_id: str,
        focus_fact_text: str,
        focus_fact_context: str,
        focus_fact_method: str,
        focus_span_start: int,
        focus_span_end: int,
        opened_at_ms: int,
        expires_at_ms: int,
        admission: str,
        queue_priority: str,
        provider_score: float | None,
        engine_type: str,
        asset_class: str,
        grounded_assets: Sequence[str],
        grounded_assets_json: str,
        watchlist_hits: Sequence[str],
        watchlist_hits_json: str,
        macro_lexicon: bool,
        storyline_key: str,
        context_line: str,
        ingest_mode: str,
        trace_id: str,
        band_keys: Sequence[str],
        now_ms: int,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO news_events (
              event_id, leader_item_id, dedupe_family, event_kind,
              comparison_fingerprint, comparison_title, leader_title,
              focus_fact_id, focus_fact_text, focus_fact_context, focus_fact_method, focus_span_start, focus_span_end,
              opened_at_ms, last_member_at_ms, expires_at_ms, member_count, admission, queue_priority,
              provider_score_max, engine_type, asset_class, grounded_assets, watchlist_hits, macro_lexicon,
              storyline_key, context_line, ingest_mode, trace_id, created_at_ms, updated_at_ms,dedupe_bands
            ) VALUES (
              %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 1, %s, %s, %s, %s, %s,
              %s::jsonb, %s::jsonb, %s, %s, %s, %s, %s, %s, %s,%s::text[]
            )
            """,
            (
                event_id,
                leader_item_id,
                dedupe_family,
                event_kind,
                comparison_fingerprint,
                comparison_title,
                leader_title,
                focus_fact_id,
                focus_fact_text,
                focus_fact_context,
                focus_fact_method,
                int(focus_span_start),
                int(focus_span_end),
                int(opened_at_ms),
                int(opened_at_ms),
                int(expires_at_ms),
                admission,
                queue_priority,
                provider_score,
                engine_type,
                asset_class,
                grounded_assets_json,
                watchlist_hits_json,
                bool(macro_lexicon),
                storyline_key,
                context_line,
                ingest_mode,
                trace_id,
                int(now_ms),
                int(now_ms),
                [f"{index}:{key}" for index, key in enumerate(band_keys)],
            ),
        )
        self.conn.execute(
            """
            INSERT INTO news_event_members
                   (event_id, item_id, joined_at_ms, match_kind, jaccard_estimate, fact_id, fact_text)
            VALUES (%s, %s, %s, 'leader', NULL, %s, %s) ON CONFLICT DO NOTHING
            """,
            (event_id, leader_item_id, int(opened_at_ms), focus_fact_id, focus_fact_text),
        )
        for symbol in grounded_assets:
            self.conn.execute(
                """
                INSERT INTO news_event_assets (symbol, event_id, market_type, opened_at_ms)
                VALUES (%s, %s, NULL, %s) ON CONFLICT DO NOTHING
                """,
                (symbol.upper().replace("XYZ-", ""), event_id, int(opened_at_ms)),
            )

    def add_member(
        self,
        *,
        event_id: str,
        item_id: str,
        joined_at_ms: int,
        match_kind: str,
        jaccard_estimate: float | None,
        provider_score: float | None,
        fact_id: str,
        fact_text: str,
        now_ms: int,
    ) -> bool:
        cursor = self.conn.execute(
            """
            INSERT INTO news_event_members
                   (event_id, item_id, joined_at_ms, match_kind, jaccard_estimate, fact_id, fact_text)
            SELECT e.event_id, %s, %s, %s, %s, %s, %s
              FROM news_events e
             WHERE e.event_id = %s
            ON CONFLICT DO NOTHING
            """,
            (item_id, int(joined_at_ms), match_kind, jaccard_estimate, fact_id, fact_text, event_id),
        )
        if not cursor.rowcount:
            return False
        self.conn.execute(
            """
            UPDATE news_events
               SET member_count = member_count + 1,
                   last_member_at_ms = GREATEST(last_member_at_ms, %s),
                   provider_score_max = GREATEST(COALESCE(provider_score_max, 0), COALESCE(%s, 0)),
                   updated_at_ms = %s
             WHERE event_id = %s
            """,
            (int(joined_at_ms), provider_score, int(now_ms), event_id),
        )
        return True

    def mark_event_published(self, *, event_id: str, now_ms: int) -> bool:
        cursor = self.conn.execute(
            "UPDATE news_events SET published_at_ms = %s, updated_at_ms = %s"
            " WHERE event_id = %s AND published_at_ms IS NULL",
            (int(now_ms), int(now_ms), event_id),
        )
        return bool(cursor.rowcount)

    def _current_event_card(self, event_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(CURRENT_EVENT_CARD_SQL, (event_id,)).fetchone()
        return dict(row) if row else None

    def _editorial_event_card(self, event_id: str) -> dict[str, Any] | None:
        """The same row, refused for an Event of a kind the public contract no longer names (#553)."""

        row = self.conn.execute(EDITORIAL_EVENT_CARD_SQL, (event_id,)).fetchone()
        return dict(row) if row else None

    def append_evidence_snapshot(
        self,
        *,
        event_id: str,
        now_ms: int,
        focus_item_id: str | None = None,
        focus_fact: Any | None = None,
    ) -> dict[str, Any]:
        material = self.evidence_snapshot_material(event_id=event_id, focus_item_id=focus_item_id)
        prepared = prepare_evidence_snapshot(
            material,
            event_id=event_id,
            now_ms=now_ms,
            focus_fact=focus_fact,
        )
        return self.append_prepared_evidence_snapshot(prepared)

    def evidence_snapshot_material(self, *, event_id: str, focus_item_id: str | None) -> dict[str, Any]:
        """Read live source facts and the compact evidence CAS state."""
        material = self._live_evidence_material(event_id)
        state = self.conn.execute(
            "SELECT evidence_version,evidence FROM news_events WHERE event_id=%s", (event_id,)
        ).fetchone()
        if state is None:
            raise ValueError("news_event_missing")
        latest = None if state["evidence"] is None else self._reconstruct_evidence(material, state)
        focus_source = None
        if focus_item_id is not None:
            focus_source = self._focus_source(focus_item_id)
        return {**material, "latest": latest, "focus_item_id": focus_item_id, "focus_source": focus_source}

    def _focus_source(self, item_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            """SELECT item_id AS leader_item_id,canonical_url AS leader_url,reporting_origin,
                      provider_metadata,provenance,published_at_ms AS leader_published_at_ms,raw_first_line
                 FROM news_items WHERE item_id=%s""",
            (item_id,),
        ).fetchone()
        if row is None:
            raise ValueError("news_event_evidence_focus_item_missing")
        return dict(row)

    def _live_evidence_material(self, event_id: str) -> dict[str, Any]:
        card = self._current_event_card(event_id)
        if card is None:
            raise ValueError("news_event_missing")
        rows = self.conn.execute(
            """SELECT m.item_id,m.fact_id,m.fact_text,m.joined_at_ms,m.match_kind,m.jaccard_estimate,
                      i.reporting_origin,i.canonical_url,i.provider_metadata,i.provenance,
                      jsonb_path_query_array(i.revisions,'$[*].revision_sha256') AS evidence_revisions
                 FROM news_event_members m JOIN news_items i USING(item_id) WHERE m.event_id=%s
                ORDER BY m.joined_at_ms,m.item_id,m.fact_id""",
            (event_id,),
        ).fetchall()
        return {"card": card, "members": [dict(row) for row in rows]}

    def _reconstruct_evidence(self, material: Mapping[str, Any], state: Mapping[str, Any]) -> dict[str, Any]:
        evidence = state["evidence"]
        version = dict(evidence["versions"][-1])
        focus = evidence["fact_scopes"][version["focus_fact_id"]]
        source = self._focus_source(str(evidence["focus_item_id"]))
        prepared = prepare_evidence_snapshot(
            {**material, "latest": None, "focus_item_id": evidence["focus_item_id"], "focus_source": source},
            event_id=str(material["card"]["event_id"]),
            now_ms=version["created_at_ms"],
            focus_fact=SimpleNamespace(**focus),
        )
        import json

        return {
            **version,
            "event_id": material["card"]["event_id"],
            "provenance": "observed",
            "release_eligible": True,
            "snapshot": json.loads(prepared["snapshot_json"]),
            "material_sha256": evidence["material_sha256"],
        }

    def append_prepared_evidence_snapshot(self, prepared: Mapping[str, Any]) -> dict[str, Any]:
        """Append version metadata under the Event lock and the version/digest CAS."""
        import json

        event_id = str(prepared["event_id"])
        lock_event(self.conn, event_id)
        row = self.conn.execute(
            "SELECT evidence_version,evidence FROM news_events WHERE event_id=%s", (event_id,)
        ).fetchone()
        if row is None:
            raise ValueError("news_event_missing")
        state = {} if row["evidence"] is None else dict(row["evidence"])
        versions = list(state.get("versions") or ())
        latest = None if not versions else versions[-1]
        actual = (None, None) if latest is None else (row["evidence_version"], latest["evidence_sha256"])
        if actual != (prepared.get("previous_version"), prepared.get("previous_sha256")):
            raise RuntimeError("news_event_evidence_snapshot_changed")
        if latest is not None and latest["evidence_sha256"] == prepared["evidence_sha256"]:
            return {
                **latest,
                "event_id": event_id,
                "snapshot": json.loads(prepared["snapshot_json"]),
                "provenance": "observed",
                "release_eligible": True,
                "material_sha256": state["material_sha256"],
            }
        document = json.loads(str(prepared["snapshot_json"]))
        fact_scopes = dict(state.get("fact_scopes") or {})
        fact_scopes.setdefault(str(prepared["focus_fact_id"]), document["focus_fact"])
        version = {
            "evidence_version": int(prepared["evidence_version"]),
            "evidence_sha256": str(prepared["evidence_sha256"]),
            "focus_fact_id": str(prepared["focus_fact_id"]),
            "created_at_ms": int(prepared["now_ms"]),
        }
        versions.append(version)
        state = {
            "material_sha256": prepared["material_sha256"],
            "focus_item_id": document["card"]["leader_item_id"],
            "fact_scopes": fact_scopes,
            "versions": versions,
        }
        self.conn.execute(
            "UPDATE news_events SET evidence_version=%s,evidence=%s::jsonb WHERE event_id=%s",
            (version["evidence_version"], _dumps(state), event_id),
        )
        return {
            **version,
            "event_id": event_id,
            "snapshot": document,
            "provenance": "observed",
            "release_eligible": True,
            "material_sha256": state["material_sha256"],
        }

    def latest_evidence_snapshot(self, event_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT evidence_version,evidence FROM news_events WHERE event_id=%s", (event_id,)
        ).fetchone()
        if row is None or row["evidence"] is None:
            return None
        return self._reconstruct_evidence(self._live_evidence_material(event_id), row)

    def event_card(self, event_id: str) -> dict[str, Any] | None:
        """Reconstruct the current evidence card and its stored version identity."""

        evidence = self.latest_evidence_snapshot(event_id)
        if evidence is None:
            return None
        snapshot = dict(evidence.get("snapshot") or {})
        if snapshot.get("schema_version") != "news_event_evidence_v3":
            raise ValueError("news_event_evidence_contract_invalid")
        card = dict(snapshot.get("card") or {})
        card.update(
            {
                "focus_fact_method": str(dict(snapshot.get("focus_fact") or {}).get("method") or "whole_item"),
                "evidence_schema_version": str(snapshot.get("schema_version") or ""),
                "evidence_version": int(evidence["evidence_version"]),
                "evidence_sha256": str(evidence["evidence_sha256"]),
                "focus_fact_id": str(evidence["focus_fact_id"]),
                "evidence_provenance": str(evidence["provenance"]),
                "evidence_release_eligible": bool(evidence["release_eligible"]),
                "evidence_members": [
                    {key: member[key] for key in ("item_id", "fact_id", "fact_text", "joined_at_ms")}
                    for member in snapshot.get("members") or ()
                ],
            }
        )
        return card

    def fact_membership(
        self,
        *,
        item_id: str,
        fact_id: str,
        event_kind: EventKind,
    ) -> Mapping[str, Any] | None:
        """Return the stable same-kind Event assignment for one admitted FactUnit."""

        return cast(
            Mapping[str, Any] | None,
            self.conn.execute(
                """
                SELECT m.event_id, m.match_kind
                 FROM news_event_members m
                  JOIN news_events e ON e.event_id = m.event_id
                 WHERE m.item_id = %s AND m.fact_id = %s AND e.event_kind = %s
                 ORDER BY e.opened_at_ms LIMIT 1
                """,
                (item_id, fact_id, event_kind),
            ).fetchone(),
        )

    def event_admission(self, event_id: str) -> Mapping[str, Any] | None:
        """Material routing identity needed for idempotent FactUnit redelivery."""

        return cast(
            Mapping[str, Any] | None,
            self.conn.execute(
                "SELECT admission, event_kind, storyline_key FROM news_events WHERE event_id = %s",
                (event_id,),
            ).fetchone(),
        )

    def event_delivery_timing(self, event_id: str) -> Mapping[str, Any] | None:
        """Source time, Reaction anchor, and local observation for reader-facing delivery."""

        row = self.conn.execute(
            """
            SELECT i.published_at_ms AS news_at_ms, e.opened_at_ms AS reaction_anchor_at_ms,
                   i.observed_at_ms, i.canonical_url
              FROM news_events e JOIN news_items i ON i.item_id = e.leader_item_id
             WHERE e.event_id = %s
            """,
            (event_id,),
        ).fetchone()
        if row is None:
            return None
        data = dict(row)
        _, artifact_at_ms = source_artifact_identity(str(data.get("canonical_url") or ""))
        return {
            "news_at_ms": int(artifact_at_ms or data["news_at_ms"]),
            # Every news_event_assets row is anchored to the Event's opened_at_ms. Reaction rows are only
            # materialized when a horizon is due, so delivery needs this durable anchor to distinguish
            # "not due" from "due but still pending" before the first Reaction row exists.
            "reaction_anchor_at_ms": int(data["reaction_anchor_at_ms"]),
            "observed_at_ms": int(data["observed_at_ms"]),
        }

    def event_member_context(self, event_id: str) -> Mapping[str, Any] | None:
        """Leader evidence needed to decide whether a later Event member is stronger."""

        return cast(
            Mapping[str, Any] | None,
            self.conn.execute(
                """
                SELECT e.admission, e.storyline_key, e.published_at_ms,
                       i.reporting_origin AS leader_origin, i.provider_metadata AS leader_provider_metadata
                  FROM news_events e JOIN news_items i ON i.item_id = e.leader_item_id
                 WHERE e.event_id = %s
                """,
                (event_id,),
            ).fetchone(),
        )

    def item_provider_score(self, item_id: str) -> Any:
        """Raw provider score used only for deterministic stronger-member comparison."""

        return self.conn.execute(
            "SELECT provider_metadata ->> 'score' AS score FROM news_items WHERE item_id = %s",
            (item_id,),
        ).fetchone()
