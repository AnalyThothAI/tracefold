# Development

This document owns design, issue, test-selection, and completion rules.

## Specify the behavior first

GitHub Issues in `AnalyThothAI/tracefold` are the durable request, PRD, and
acceptance surface. A non-trivial change starts with one current issue that
states:

- the problem and observable outcome;
- invariants and public contracts that must remain stable;
- allowed hard cuts and explicitly deleted compatibility paths;
- implementation boundaries;
- verification and cutover evidence.

Update the issue when a decision changes. Do not create parallel planning
archives or historical design diaries in `docs/`.

Before adding a service, table, worker, score, or contract, trace the existing
provider input through PostgreSQL fact, durable target, current row, and
consumer. Extend the current owner unless lifecycle or responsibility is
genuinely different. New tables, workers, model-backed products, probabilistic
outputs, or evaluation control planes require an explicit current need.

## Package design

Business capabilities are exported from `tracefold.market`,
`tracefold.news`, `tracefold.macro`, and `tracefold.notifications`. Code outside
the owning package imports only those roots. Keep internal modules cohesive and
move behavior behind the root interface instead of adding forwarding modules,
aliases, or compatibility packages.

PostgreSQL material facts and public HTTP/WS/CLI contracts are migration
boundaries. Internal Python imports are not compatibility contracts. Hard cuts
delete the old path and update all consumers in the same change.

## Tests

| Lane | Location | Proves |
|---|---|---|
| Architecture | `tests/architecture/` | package shape, dependency direction, durable ownership |
| Contract | `tests/contract/` | public HTTP/WS/CLI and generated schemas |
| Integration | `tests/integration/` | real PostgreSQL and composed service behavior |
| Golden | `tests/golden/` | curated fact-to-product expectations |
| E2E | `tests/e2e/` | running process boundaries |
| Frontend | `web/tests/` | UI, route, model, and frontend architecture behavior |

Prefer behavior at a maintained public or persistence seam. Do not preserve
tests that assert private file layout, source text, mock call choreography, or
implementation detail. There is no coverage-percentage gate.

Select commands by risk:

- schema or repository behavior: focused real-PostgreSQL integration tests;
- HTTP/WS/CLI behavior: contract tests plus regenerated artifacts;
- workers: claim, lease, retry/terminal, restart catch-up, idempotency,
  single-writer, and external-I/O transaction boundaries;
- UI: scoped tests, lint, typecheck, build, and a browser check when visual or
  interactive behavior changes;
- documentation: bounded surface and link checks;
- generated files: run the owning generator and verify a clean second run.

`make check` is a fast static/frontend/architecture/contract bundle, not a
universal completion mandate. Run only the additional lanes that cross the
changed seam and report omitted evidence honestly.

### News WorldMonitor parity evaluation

News identity, classification, importance, and Brief selection are frozen to
the implementation copied from WorldMonitor commit
`f73de5b7dde76ff292f800d7d06f3529d2178d43`. Tracefold adds persistence,
stable Story IDs, scheduled acquisition, `reporting_origin` corroboration, and
immutable Chinese Brief publications; it does not add a second identity or
scoring policy.

`tests/test_news_worldmonitor_parity.py` is the executable parity suite. Its 79
tests cover positive and negative title pairs, exact-title and containment
merges, CJK features, hot buckets, order independence, classification,
historical exclusions, importance rounding, and source-diverse Top-8
selection. Run it with:

```bash
uv run pytest -q tests/test_news_worldmonitor_parity.py
```

The release evaluation additionally replays the current production NewsItem
corpus in isolated PostgreSQL. The review artifact must state its source
cutoff, item/Story/singleton/multi-member/origin/category counts, every
multi-member cluster, and the highest-similarity non-merged pairs. It is a
distribution review, not a compression target. The known Ebola
3,200-infections/1,405-deaths pair remains split at approximately `0.509`
similarity because the frozen threshold is `0.615`; changing that result
requires a new shared-corpus specification, never a private production patch.

Cutover acceptance requires a destructive empty-News-schema cold start,
exactly ten News tables, 96 synchronized sources, exactly three News workers,
real RSS observations and NewsItems, deterministic Story membership, all four
public endpoints, one valid Chinese Brief, provider-failure last-known-good
retention, measured acquisition/projection latency, and browser verification
of Feed, Story, Brief, and Sources.

## Generated contracts

`docs/generated/` contains only reproducible outputs:

```bash
make docs-generated
make regen-contract
```

Generated OpenAPI and frontend types change in the same commit as their source.

## Completion

A change is complete only when:

- observable behavior and durable invariants have direct successful evidence;
- generated outputs are current;
- public contracts and PostgreSQL fact semantics remain intact or change
  through an explicitly approved migration;
- old names, files, imports, and compatibility paths are gone;
- deployment/cutover evidence is recorded for runtime changes;
- omitted lanes and remaining risks are named without manufacturing green
  results through skips or compatibility mocks.
