"""Current PostgreSQL and Python content identity agreement."""

from __future__ import annotations

import pytest
from psycopg.types.json import Jsonb

from tests.postgres_test_utils import connect_postgres_test
from tracefold.news.artifact_identity import canonical_sha

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("postgres_clone_dsn")]


def test_news_canonical_json_hash_matches_python_for_nested_unicode_payload() -> None:
    payload = {
        "z": [3, {"中文": "证据", "boolean": True}, None],
        "a": {"nested": [2, 1], "value": -42},
    }
    conn = connect_postgres_test(read_only=False)
    try:
        row = conn.execute(
            "SELECT encode(sha256(convert_to(news_canonical_jsonb(%s), 'UTF8')), 'hex') AS sha",
            (Jsonb(payload),),
        ).fetchone()
    finally:
        conn.close()

    assert row["sha"] == canonical_sha(payload)
