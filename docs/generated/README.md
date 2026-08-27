# Generated

> **DO NOT HAND-EDIT files in this directory.** They are regenerated from the source of truth listed in each file's header. Edit the source, then run the regenerator.

## Regenerate

```bash
make docs-generated
make regen-contract
```

These commands run the source generators below:

| File | Source | Script |
|------|--------|--------|
| `db-schema.md` | Alembic head + `pg_catalog` introspection | `scripts/regen_db_schema.py` |
| `cli-help.md` | `tracefold --help` recursively | `scripts/regen_cli_help.py` |
| `openapi.json` | mounted FastAPI routes and schemas | `scripts/regen_openapi.py` |

CI verifies each current generated contract with its owning generator. Historical
source layouts and dependency-lock snapshots are not generated contracts; release
identity is protected directly by the narrow Program identity contract.
