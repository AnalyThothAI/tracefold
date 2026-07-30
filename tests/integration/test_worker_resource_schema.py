from tests.postgres_test_utils import connect_postgres_test, prepare_postgres_database


def test_worker_resource_hard_cut_schema_is_typed_and_domain_owned():
    prepare_postgres_database()
    conn = connect_postgres_test(read_only=True)
    try:
        rows = conn.execute(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_name = ANY(%s)
            ORDER BY table_name
            """,
            (
                [
                    "worker_runtime_status",
                    "persisted_live_events",
                    "radar_projection_frontiers",
                    "radar_source_edges",
                    "macro_module_frontiers",
                    "macro_dataset_projection_states",
                    "news_projection_frontiers",
                    "news_identity_features",
                    "news_similarity_edges",
                    "token_profile_projection_frontiers",
                    "model_generation_frontiers",
                ],
            ),
        ).fetchall()
    finally:
        conn.close()

    assert [row["table_name"] for row in rows] == [
        "macro_dataset_projection_states",
        "macro_module_frontiers",
        "model_generation_frontiers",
        "news_identity_features",
        "news_projection_frontiers",
        "news_similarity_edges",
        "persisted_live_events",
        "radar_projection_frontiers",
        "radar_source_edges",
        "token_profile_projection_frontiers",
        "worker_runtime_status",
    ]

    conn = connect_postgres_test(read_only=True)
    try:
        radar_pk = conn.execute(
            """
            SELECT array_agg(attribute.attname ORDER BY key_columns.ordinality) AS columns
            FROM pg_constraint constraint_row
            JOIN LATERAL unnest(constraint_row.conkey) WITH ORDINALITY AS key_columns(attnum, ordinality)
              ON true
            JOIN pg_attribute attribute
              ON attribute.attrelid = constraint_row.conrelid
             AND attribute.attnum = key_columns.attnum
            WHERE constraint_row.conrelid = 'radar_projection_frontiers'::regclass
              AND constraint_row.contype = 'p'
            """
        ).fetchone()
    finally:
        conn.close()

    assert radar_pk["columns"] == [
        "target_type",
        "target_id",
        "window_key",
        "venue",
    ]
