from tests.postgres_test_utils import connect_postgres_test, prepare_postgres_database


def test_worker_runtime_v2_schema_keeps_only_native_domain_state_and_one_runtime_row():
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
                    "token_radar_current",
                    "macro_module_frontiers",
                    "macro_dataset_projection_states",
                    "token_profile_projection_frontiers",
                    "model_generation_frontiers",
                    "queue_terminal_events",
                    "worker_queue_terminal_events",
                    "workers_runtime",
                ],
            ),
        ).fetchall()
    finally:
        conn.close()

    assert [row["table_name"] for row in rows] == [
        "macro_dataset_projection_states",
        "macro_module_frontiers",
        "persisted_live_events",
        "queue_terminal_events",
        "token_profile_projection_frontiers",
        "token_radar_current",
        "workers_runtime",
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
            WHERE constraint_row.conrelid = 'token_radar_current'::regclass
              AND constraint_row.contype = 'p'
            """
        ).fetchone()
    finally:
        conn.close()

    assert radar_pk["columns"] == ["singleton_key"]
