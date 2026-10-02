"""P3 preserves source projections and rejects unsafe predecessor facts transactionally."""

from __future__ import annotations

import hashlib
import json
from contextlib import closing

import pytest
from alembic import command
from psycopg.errors import CheckViolation

from tests.fixtures.news_semantic_0422 import persist_update, seed_event
from tests.postgres_test_utils import (
    connect_postgres_test,
    postgres_migration_test_dsn,
    prepare_test_migration_database,
)
from tests.support.news_0424_sql import (
    ANALYSES_SQL,
    ANALYSIS_HEADS_SQL,
    CLAIM_LINKS_SQL,
    EVIDENCE_VERSIONS_SQL,
    ITEM_REVISIONS_SQL,
    SEMANTIC_JOBS_SQL,
    SEMANTIC_RESULTS_SQL,
)
from tests.support.news_event_updates import first_update, raised_update
from tests.support.news_update_pg import EVENT, STAMP
from tracefold.news.storage.events import semantic_material
from tracefold.news.storage.notification_context import NotificationContextStorage
from tracefold.news.storage.sql_values import _dumps
from tracefold.news.updates.identity import digest
from tracefold.platform.postgres.migrations import alembic_config

pytestmark = [pytest.mark.integration, pytest.mark.migration, pytest.mark.usefixtures("postgres_migration_dsn")]
SOURCE, TARGET = "20261001_0422", "20261001_0423"


@pytest.fixture
def source(postgres_migration_dsn):
    with closing(connect_postgres_test()) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.commit()
    prepare_test_migration_database(postgres_migration_dsn)
    config = alembic_config()
    config.attributes["database_url"] = postgres_migration_test_dsn()
    command.upgrade(config, SOURCE)
    seed_event()
    first = first_update(EVENT)
    second = raised_update(first)
    # The old writer loops in document order and ON CONFLICT keeps the first
    # relation for the natural link key, even when another change repeats it.
    second = second.model_copy(
        update={"changes": (*second.changes, second.changes[-1].model_copy(update={"relation": "conflicts"}))}
    )
    with closing(connect_postgres_test()) as conn, conn.transaction():
        persist_update(conn, first)
        persist_update(conn, second)
        repaired = second.model_dump(mode="json")
        repair_revision = digest("scope-repair")
        repaired.update(
            content_revision=repair_revision,
            previous_content_revision=second.content_revision,
            input_revision=3,
            adopted_at_ms=STAMP + 1200,
            changes=[],
        )
        conn.execute(
            """INSERT INTO news_head_scope_repairs(repair_id,event_id,previous_content_revision,content_revision,
                 claim_refs,proof,projection_version,recorded_at_ms)
               VALUES ('repair',%s,%s,%s,%s,'{}','scope-test',%s)""",
            (EVENT, second.content_revision, repair_revision, [second.claims[0].ref], STAMP + 1200),
        )
        conn.execute(
            """INSERT INTO news_event_updates(event_id,content_revision,input_revision,previous_content_revision,
                 adopted_at_ms,scope_repair_id,document) VALUES (%s,%s,3,%s,%s,'repair',%s::jsonb)""",
            (EVENT, repair_revision, second.content_revision, STAMP + 1200, json.dumps(repaired)),
        )
        conn.execute(
            """UPDATE news_event_update_heads SET content_revision=%s,input_revision=3,
                 update_ref=news_identity('update',jsonb_build_array(event_id,%s::text)),adopted_at_ms=%s""",
            (repair_revision, repair_revision, STAMP + 1200),
        )
        for change in second.changes:
            if (
                change.previous_ref
                and change.previous_ref != change.current_ref
                and change.relation in {"equivalent", "adds_information", "real_world_change", "corrects", "conflicts"}
            ):
                conn.execute(
                    "INSERT INTO news_claim_links VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                    (
                        second.ref,
                        change.current_ref,
                        change.previous_ref,
                        change.relation,
                        EVENT,
                        EVENT,
                        second.adopted_at_ms,
                    ),
                )
        conn.execute(
            """INSERT INTO news_semantic_observations(result_id,work_id,event_id,input_revision,input_sha256,
                 program_identity,completed_at_ms,understanding,read_refs)
               VALUES ('unadopted','work-unadopted',%s,3,%s,'test-program',%s,'{}',ARRAY['read:one'])""",
            (EVENT, digest("input"), STAMP + 1000),
        )
        conn.execute("INSERT INTO news_semantic_checkpoints VALUES ('work:with:colons','extraction','{}',%s)", (STAMP,))
        conn.execute(
            "INSERT INTO news_event_bands VALUES (2,'key:with:colons',%s,'general',%s)",
            (EVENT, STAMP + 86_400_000),
        )
        conn.execute(
            """UPDATE news_semantic_work SET wanted_revision=3,done_revision=2,attempts=2,lease_token='owner',
                 leased_until_ms=%s,last_error_code='deferred',last_outcome='deferred',
                 processed_read_refs=ARRAY['processed'],failed_read_refs=ARRAY['failed'],attempt_read_refs=ARRAY['attempt'],
                 extra_read_state='reserved',extra_read_target_ref='target',reanalysis_reason='inspect',
                 reanalysis_read_ref='read',reanalysis_head_ref=%s WHERE event_id=%s""",
            (STAMP + 60_000, second.ref, EVENT),
        )
        conn.execute(
            """INSERT INTO news_item_revisions(item_id,revision_sha256,content_sha256,previous_revision_sha256,
                 revision_sequence,evidence_text,provider_params,reporting_origin,canonical_url,source_artifact_id,
                 published_at_ms,received_at_ms)
               VALUES (%s,%s,%s,%s,1,'revised body','{}','Reuters','https://example.com/source','source',%s,%s)""",
            (f"it-{EVENT}", digest("revision"), digest("body"), digest("original"), STAMP, STAMP + 100),
        )
    return config


def rows(conn, query):
    return sorted(
        (dict(row) for row in conn.execute(query).fetchall()), key=lambda row: json.dumps(row, sort_keys=True)
    )


def test_twenty_read_projections_survive_the_populated_cut(source):
    pairs = [
        (
            "SELECT event_id,content_revision,input_revision,previous_content_revision,adopted_at_ms,"
            "observation_result_id,scope_repair_id,document FROM news_event_updates",
            ANALYSES_SQL,
        ),
        ("SELECT * FROM news_event_update_heads", ANALYSIS_HEADS_SQL),
        (
            "SELECT result_id,work_id,event_id,input_revision,input_sha256,program_identity,"
            "completed_at_ms,understanding,"
            "read_refs,reanalysis_reason,reanalysis_head_ref FROM news_semantic_observations",
            SEMANTIC_RESULTS_SQL,
        ),
        ("SELECT * FROM news_semantic_work", SEMANTIC_JOBS_SQL),
        ("SELECT * FROM news_item_revisions", ITEM_REVISIONS_SQL),
        ("SELECT * FROM news_claim_links", CLAIM_LINKS_SQL),
        (
            "SELECT event_id,evidence_version,evidence_sha256,focus_fact_id,created_at_ms,provenance,release_eligible "
            "FROM news_event_evidence_snapshots",
            EVIDENCE_VERSIONS_SQL,
        ),
    ]
    queries = []
    for old, new in pairs:
        # Project exactly the public/source columns; new identity metadata is intentionally additional.
        with closing(connect_postgres_test()) as conn:
            columns = list(conn.execute(old).description)
        names = ",".join(column.name for column in columns)
        queries.append((old, f"SELECT {names} FROM ({new}) projected"))
    extra = [
        (
            "SELECT result_id FROM news_semantic_observations WHERE input_revision=3",
            "SELECT analysis_id AS result_id FROM news_analyses WHERE origin='semantic' AND input_revision=3",
        ),
        (
            "SELECT content_revision FROM news_event_updates ORDER BY adopted_at_ms LIMIT 1",
            "SELECT content_revision FROM news_analyses WHERE adopted_at_ms IS NOT NULL ORDER BY adopted_at_ms LIMIT 1",
        ),
        (
            "SELECT work_id,stage,document,created_at_ms FROM news_semantic_checkpoints",
            "SELECT substr(cache_key,32) AS work_id,'extraction'::text AS stage,answer AS document,created_at_ms "
            "FROM news_judgment_cache WHERE cache_key LIKE 'semantic_checkpoint:%'",
        ),
        (
            "SELECT band_index,band_key,event_id,dedupe_family,expires_at_ms FROM news_event_bands",
            "SELECT split_part(band,':',1)::smallint AS band_index,substr(band,strpos(band,':')+1) AS band_key,"
            "event_id,dedupe_family,expires_at_ms FROM news_events CROSS JOIN LATERAL unnest(dedupe_bands) band",
        ),
        (
            "SELECT count(*) AS n FROM news_head_scope_repairs",
            "SELECT count(*) AS n FROM news_analyses WHERE origin='scope_repair'",
        ),
        (
            "SELECT count(*) AS n FROM news_semantic_observations",
            "SELECT count(*) AS n FROM news_analyses WHERE origin='semantic'",
        ),
    ]
    queries.extend(extra)
    # Seven filtered projections exercise the same fields through their bounded per-event readers.
    for index, (old, new) in enumerate(queries[:7]):
        predicate = (
            f"item_id='it-{EVENT}'"
            if index == 4
            else (f"current_event_id='{EVENT}'" if index == 5 else f"event_id='{EVENT}'")
        )
        queries.append(
            (f"SELECT * FROM ({old}) filtered WHERE {predicate}", f"SELECT * FROM ({new}) filtered WHERE {predicate}")
        )
    assert len(queries) == 20
    with closing(connect_postgres_test()) as conn:
        expected = [rows(conn, old) for old, _ in queries]
        prior_snapshot = conn.execute(
            "SELECT snapshot FROM news_event_evidence_snapshots ORDER BY evidence_version DESC LIMIT 1"
        ).fetchone()["snapshot"]
    command.upgrade(source, TARGET)
    with closing(connect_postgres_test()) as conn:
        assert [rows(conn, new) for _, new in queries] == expected
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM information_schema.tables WHERE table_schema='public' "
                "AND table_type='BASE TABLE'"
            ).fetchone()["n"]
            == 36
        )
        assert conn.execute("SELECT reloptions FROM pg_class WHERE oid='news_events'::regclass").fetchone()[
            "reloptions"
        ] == ["fillfactor=85"]
        evidence = conn.execute("SELECT evidence FROM news_events").fetchone()["evidence"]
        assert (
            evidence["material_sha256"]
            == hashlib.sha256(_dumps(semantic_material(prior_snapshot)).encode()).hexdigest()
        )
        links = conn.execute(CLAIM_LINKS_SQL).fetchall()
        assert len(links) == 1 and links[0]["relation"] == "real_world_change"
        reader = NotificationContextStorage(conn, updates=None)
        recalled = reader._claim_links([links[0]["current_ref"]], as_of_ms=STAMP + 2000)
        assert len(recalled) == 1 and recalled[0]["relation"] == "real_world_change"


@pytest.mark.parametrize(
    ("statement", "code"),
    [
        ("UPDATE news_event_bands SET expires_at_ms=1", "p3_band_event_mismatch"),
        ("UPDATE news_event_update_heads SET input_revision=9", "p3_head_update_mismatch"),
        (
            "INSERT INTO news_judgment_cache VALUES ('semantic_checkpoint:extraction:work:with:colons','{}',1)",
            "p3_checkpoint_key_collision",
        ),
        (
            "INSERT INTO news_jobs(job_kind,subject_id,state,detail,created_at_ms,updated_at_ms) "
            "VALUES ('semantic','orphan','pending','{}',1,1)",
            "p3_semantic_jobs_exist",
        ),
    ],
)
def test_prechecks_roll_back_unsafe_source(source, statement, code):
    with closing(connect_postgres_test()) as conn:
        conn.execute(statement)
        conn.commit()
    with pytest.raises(Exception, match=code):
        command.upgrade(source, TARGET)
    with closing(connect_postgres_test()) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"] == SOURCE
        assert conn.execute("SELECT to_regclass('news_analyses') AS relation").fetchone()["relation"] is None


def test_analysis_trigger_allows_one_adoption_and_rejects_source_mutation(source):
    command.upgrade(source, TARGET)
    with closing(connect_postgres_test()) as conn, conn.transaction():
        with pytest.raises(CheckViolation, match="news_analysis_immutable"), conn.transaction():
            conn.execute("UPDATE news_analyses SET input_sha256='changed' WHERE analysis_id='unadopted'")
        conn.execute("""UPDATE news_analyses SET content_revision='once',update_ref='update:once',
                     adopted_at_ms=1,document='{}' WHERE analysis_id='unadopted'""")
        with pytest.raises(CheckViolation, match="news_analysis_immutable"), conn.transaction():
            conn.execute("UPDATE news_analyses SET document='{}' WHERE analysis_id='unadopted'")
