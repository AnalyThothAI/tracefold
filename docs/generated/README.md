# Generated contract references

[Handbook](../README.md) · [Contracts](../CONTRACTS.md)

Generated outputs below are derived from executable contracts, not hand-edited.
This README is their maintained navigation and regeneration procedure.

| Output | Authority | Generator |
| --- | --- | --- |
| [cli-help.md](cli-help.md) | Actual CLI parsers/help | `scripts/regen_cli_help.py` |
| [openapi.json](openapi.json) | Mounted FastAPI routes and response schemas | `scripts/regen_openapi.py` |
| [db-schema.md](db-schema.md) | Isolated migrated PostgreSQL schema and catalog introspection | `scripts/regen_db_schema.py` |

## Regenerate only the changed contract

```bash
uv run python scripts/regen_cli_help.py --check
make regen-contract
```

`regen-contract` also owns committed frontend OpenAPI types. For database docs,
explicitly set `TRACEFOLD_TEST_POSTGRES_DSN` to an **isolated database already at
the correct head**, then run `uv run python scripts/regen_db_schema.py`. Without
that explicit test DSN the generator can read operator config; do not use it
against production merely to edit documentation.

`make docs-generated` includes database introspection and therefore requires the
same prepared isolated resource. Preserve the generated ordering and constraints;
a changed hand-written guide is not a reason to rewrite a schema snapshot.
[Testing](../TESTING.md) owns the associated resource-backed verification.

## Documentation-only checks

```bash
python scripts/check_mandatory_docs_links.py
python scripts/sync_agent_router.py --check
```

These commands need no database, model, account or documentation service. Source
navigation belongs in [Architecture](../ARCHITECTURE.md) and the module guides;
there is no separate generated function catalog to keep synchronized.
