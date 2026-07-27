from __future__ import annotations

import os
from typing import Any

DEFAULT_TEST_POSTGRES_IMAGE = (
    "postgres:18-bookworm@sha256:1961f96e6029a02c3812d7cb329a3b03a3ac2bb067058dec17b0f5596aca9296"
)
TEST_POSTGRES_IMAGE = os.environ.get("TRACEFOLD_TEST_POSTGRES_IMAGE", DEFAULT_TEST_POSTGRES_IMAGE)
TRACEFOLD_POSTGRES_COMMAND = [
    "postgres",
    "-c",
    "shared_preload_libraries=pg_stat_statements",
    "-c",
    "compute_query_id=on",
    "-c",
    "pg_stat_statements.track=all",
]


def tracefold_postgres_container(postgres_container_cls: type[Any]) -> Any:
    return postgres_container_cls(TEST_POSTGRES_IMAGE).with_command(TRACEFOLD_POSTGRES_COMMAND)
