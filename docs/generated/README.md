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
| `refactor-baseline-9441ce99.json` | Issue #162 behavior/runtime contracts and pre-refactor structure | `python -m tests.support.refactor_baseline` |

CI verifies current generated contracts and the behavior/runtime section of the
Issue #162 baseline. Its historical structure is read from the named source
revision so later directory PRs can measure improvement against it. Run the
owning module without `--check` to reproduce the file and inspect its diff.
