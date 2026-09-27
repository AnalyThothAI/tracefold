# Generated references

[Handbook](../README.md) · [Contracts](../CONTRACTS.md)

Generated outputs below are not hand-edited. This README describes their owners;
it is maintained prose, not generated application configuration.

| Output | Source of truth | Generator |
| --- | --- | --- |
| [repository-map.md](repository-map.md) | Git-tracked paths and Python AST docstrings/declarations; no application imports | `scripts/regen_repository_map.py` |
| [cli-help.md](cli-help.md) | Actual CLI parsers and help | `scripts/regen_cli_help.py` |
| [db-schema.md](db-schema.md) | Isolated migrated PostgreSQL schema and catalog introspection | `scripts/regen_db_schema.py` |
| [openapi.json](openapi.json) | Mounted FastAPI routes and response schemas | `scripts/regen_openapi.py` |

## Local documentation checks

Stage intended new files before regenerating the repository map. It intentionally
does not scan ignored files, operator homes, credentials or arbitrary untracked
artifacts. Python files are parsed as syntax, never imported for inventory.

```bash
python scripts/regen_repository_map.py --write
python scripts/regen_repository_map.py --check
python scripts/check_mandatory_docs_links.py
python scripts/sync_agent_router.py --check
```

The link check covers current guides, retained research/reports, agent routing,
CONTEXT and notebook Markdown. It checks local files, Markdown heading anchors and
reference links, not external-site availability. Literal fenced examples are not
actual navigation. It does not render Mermaid diagrams.

## Full generated contracts

```bash
make docs-generated
make regen-contract
```

`docs-generated` includes database introspection and therefore needs the documented
isolated test PostgreSQL environment. A docs-only map/link check does not. Contract
generation also owns committed frontend OpenAPI types. Do not start services or
rewrite unrelated schema snapshots merely to change a paragraph.
[Testing](../TESTING.md) names the applicable verification lanes.
