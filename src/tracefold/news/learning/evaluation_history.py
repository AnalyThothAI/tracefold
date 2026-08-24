"""Arm-local reader receipts and shared history assembly for CandidateEvaluator."""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..models import base_symbol
from ..reader_history import TARGETED_HISTORY_WINDOW_MS, ReaderHistorySnapshot, build_reader_history


@dataclass(slots=True)
class Receipt:
    event_id: str
    at_ms: int
    storyline_key: str
    magnitude: int
    direction: str
    headline_zh: str
    comparison_title: str = ""
    comparison_fingerprint: str = ""
    family: str = "general"
    event_type: str = ""
    grounded_assets: tuple[str, ...] = ()
    assets: tuple[str, ...] = ()
    canonical_assets: tuple[str, ...] = ()

    def as_told_row(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "at_ms": self.at_ms,
            "storyline_key": self.storyline_key,
            "comparison_title": self.comparison_title,
            "comparison_fingerprint": self.comparison_fingerprint,
            "family": self.family,
            "event_type": self.event_type,
            "magnitude": self.magnitude,
            "direction": self.direction,
            "headline_zh": self.headline_zh,
            "grounded_assets": list(self.grounded_assets),
            "assets": list(self.assets),
            "canonical_assets": list(self.canonical_assets),
        }


@dataclass(slots=True)
class ArmState:
    receipts: deque[Receipt] = field(default_factory=deque)
    observations: list[dict[str, Any]] = field(default_factory=list)

    def expire(self, at_ms: int) -> None:
        cutoff = at_ms - TARGETED_HISTORY_WINDOW_MS
        while self.receipts and self.receipts[0].at_ms < cutoff:
            self.receipts.popleft()


class EvaluationReaderHistory:
    def __init__(self, conn: Any) -> None:
        self._conn = conn
        self._symbol_aliases: dict[str, str] | None = None

    def _alias_map(self) -> dict[str, str]:
        if self._symbol_aliases is None:
            rows = self._conn.execute("SELECT alias, base_symbol FROM news_symbol_aliases").fetchall()
            self._symbol_aliases = {
                str(row["alias"]): base_symbol(str(row["base_symbol"]))
                for row in rows
                if row.get("alias") and row.get("base_symbol")
            }
        return self._symbol_aliases

    def canonical_assets(self, symbols: Sequence[str]) -> tuple[str, ...]:
        aliases = self._alias_map()
        return tuple(
            sorted(
                {
                    aliases.get(str(symbol), aliases.get(base_symbol(str(symbol)), base_symbol(str(symbol))))
                    for symbol in symbols
                    if symbol
                }
            )
        )

    def build(self, case: Mapping[str, Any], state: ArmState) -> ReaderHistorySnapshot:
        event = dict((case.get("snapshot") or {}).get("card") or {})
        grounded = tuple(str(value) for value in event.get("grounded_assets") or ())
        return build_reader_history(
            [receipt.as_told_row() for receipt in state.receipts],
            now_ms=int(case["opened_at_ms"]),
            family=str(event.get("family") or "general"),
            comparison_fingerprint=str(event.get("comparison_fingerprint") or ""),
            canonical_assets=self.canonical_assets(grounded),
        )

    def seed_receipts(
        self,
        *,
        from_ms: int,
        epoch_started_at_ms: int,
        cohort: bool,
        program_version: str,
        program_sha256: str,
        bundle_sha: str,
    ) -> tuple[dict[str, Any], ...]:
        """Project the same latest delivered verdict production uses into an evaluator receipt source."""

        rows = self._conn.execute(
            """
            SELECT v.event_id, d.settled_at_ms AS at_ms, e.storyline_key,
                   COALESCE(e.comparison_title, '') AS comparison_title,
                   COALESCE(e.comparison_fingerprint, '') AS comparison_fingerprint,
                   COALESCE(e.family, 'general') AS family,
                   COALESCE(v.verdict ->> 'event_type', '') AS event_type,
                   COALESCE((v.verdict ->> 'magnitude')::int, 0) AS magnitude,
                   COALESCE(v.verdict ->> 'direction', 'unclear') AS direction,
                   COALESCE(NULLIF(d.card #>> '{header,title,content}', ''), v.verdict ->> 'headline_zh', '')
                     AS headline_zh,
                   COALESCE(e.grounded_assets, '[]'::jsonb) AS grounded_assets,
                   COALESCE(
                     (SELECT jsonb_agg(asset ->> 'symbol')
                        FROM jsonb_array_elements(COALESCE(v.verdict -> 'assets', '[]'::jsonb)) AS asset
                       WHERE asset ->> 'symbol' IS NOT NULL),
                     '[]'::jsonb
                   ) AS assets,
                   COALESCE(
                     (SELECT jsonb_agg(base_symbol ORDER BY base_symbol)
                        FROM (SELECT DISTINCT COALESCE(a.base_symbol, ea.symbol) AS base_symbol
                                FROM news_event_assets ea
                                LEFT JOIN news_symbol_aliases a ON a.alias = ea.symbol
                               WHERE ea.event_id = e.event_id) bases),
                     '[]'::jsonb
                   ) AS canonical_assets
              FROM news_deliveries d
              JOIN news_events e ON e.event_id = d.event_id
              JOIN LATERAL (
                SELECT candidate.*
                  FROM (
                    SELECT scoped.* FROM news_verdicts scoped
                     WHERE scoped.event_id = e.event_id
                       AND scoped.stage = 'triage'
                       AND scoped.final_decision IN ('push', 'escalate')
                     OFFSET 0
                  ) candidate
                 ORDER BY candidate.created_at_ms DESC, candidate.policy_version DESC
                 LIMIT 1
              ) v ON true
             WHERE d.kind = 'first' AND d.state = 'sent'
               AND d.settled_at_ms >= %s AND d.settled_at_ms < %s
               AND (%s IS FALSE OR (
                     v.program_version = %s AND v.program_sha256 = %s
                     AND v.trace #>> '{agent_assignment,bundle_sha}' = %s
                   ))
             ORDER BY d.settled_at_ms, v.event_id
            """,
            (
                max(epoch_started_at_ms, from_ms - TARGETED_HISTORY_WINDOW_MS),
                from_ms,
                cohort,
                program_version,
                program_sha256,
                bundle_sha,
            ),
        ).fetchall()
        return tuple(dict(row) for row in rows)


def receipt_from_output(*, event_id: str, at_ms: int, output: Mapping[str, Any], verdict: Mapping[str, Any]) -> Receipt:
    return Receipt(
        event_id=event_id,
        at_ms=at_ms,
        storyline_key=str(output.get("storyline_key") or "macro:general"),
        magnitude=int(verdict.get("magnitude") or 0),
        direction=str(verdict.get("direction") or "unclear"),
        headline_zh=str(verdict.get("headline_zh") or ""),
        comparison_title=str(output.get("comparison_title") or ""),
        comparison_fingerprint=str(output.get("comparison_fingerprint") or ""),
        family=str(output.get("family") or "general"),
        event_type=str(verdict.get("event_type") or ""),
        grounded_assets=tuple(str(value) for value in output.get("grounded_assets") or ()),
        assets=tuple(
            str(asset.get("symbol") or "") for asset in verdict.get("assets") or () if isinstance(asset, Mapping)
        ),
        canonical_assets=tuple(str(value) for value in output.get("canonical_assets") or ()),
    )


__all__ = ["ArmState", "EvaluationReaderHistory", "Receipt", "receipt_from_output"]
