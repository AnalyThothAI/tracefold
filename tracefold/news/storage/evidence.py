"""News-owned material reads. Callers freeze and select outside transactions."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

ITEM_MATERIAL_COLUMNS = """item_id, source_artifact_id, canonical_url, reporting_origin, published_at_ms,
    provider_params_available_at_ms, provider_params_sha256, evidence_text, evidence_text_sha256"""


class EvidenceStorage:
    conn: Any

    def evidence_material(self, item_ids: Sequence[str]) -> list[dict[str, Any]]:
        # Read exactly the immutable Items selected by the lightweight query, never
        # re-resolve a mutable Event leader between the two reads.
        if not item_ids:
            return []
        return [
            dict(row)
            for row in self.conn.execute(
                "SELECT " + ITEM_MATERIAL_COLUMNS + " FROM news_items WHERE item_id=ANY(%s)",  # noqa: S608
                (list(item_ids),),
            ).fetchall()
        ]
