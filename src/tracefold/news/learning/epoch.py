"""What migration `0318` wrote when it opened `program_v8`.

Its own module because two readers need it and neither should import the other to get it: the ledger
validates the persisted epoch row against these before any evidence is treated as eligible, and the
evaluator names them in the trusted root it publishes.
"""

from __future__ import annotations

# Must equal the `reset_reason` migration 0318 wrote for `program_v8`. The evaluator validates the
# epoch row field by field, so a bumped epoch with a stale reason here fails every evaluation.
LEARNING_EPOCH_RESET_REASON = "single_instruction_seed_and_self_owned_transport_identity_migration"
# What migration 0318 wrote when it opened `program_v8` — deliberately not the runtime constants. The
# Program root is re-issued *inside* an epoch whenever its serialization or factory changes without
# changing which evidence is eligible (#173/#174, #190, #193), so the row keeps naming what it was
# opened with, exactly as `baseline_program_sha256` already does. Asserting these still detects migration
# drift and ledger corruption; asserting today's values against them would fail a correctly migrated
# database on every in-epoch re-issue.
LEARNING_EPOCH_OPENED_FACTORY_ID = "tracefold.news.program.factory_v8"
LEARNING_EPOCH_OPENED_ARTIFACT_SCHEMA_VERSION = "news_program_strategy_artifact_v1"

__all__ = [
    "LEARNING_EPOCH_OPENED_ARTIFACT_SCHEMA_VERSION",
    "LEARNING_EPOCH_OPENED_FACTORY_ID",
    "LEARNING_EPOCH_RESET_REASON",
]
