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

### News Story Identity v2 frozen evaluation

`news_story_identity_v2_proof_ladder` is released against the actual
`NewsRepository` → PostgreSQL → `NewsInterface` seam, not a second clustering
implementation. The primary fixture
`tests/fixtures/news_story_identity_golden.json` has 35 isolated cases and 70
reports: 19 same-event positive pairs, 16 hard-negative pairs, and 22 cases
adjudicated from the 2026-07-27 production ambiguity audit. It covers exact
title, truncation, containment, paraphrase, cross-language reports, compatible
numeric revisions, syndication, stage, temporal episode, named event,
actor-direction, identity number, roundup, reaction, and distinct action. A
secondary WorldMonitor-reference fixture adds 13 positive and 10 negative
pairs through the same seam.

The read-only pre-cut production snapshot was Alembic `20260726_0198`, 581
Articles, 564 Stories, and 226 `ambiguous_new_story` decisions. Expanding the
old fixture first produced candidate recall `1.0` but five false merges and five
false splits: pairwise precision/recall `0.736842`, B-cubed
precision/recall/purity `0.928571`. The false merges came from treating
correlated sparse anchors as independent event proof; false splits exposed
missing or under-normalized event actions and objects.

The correction keeps the admission threshold fixed and instead:

1. requires an action plus an independent event object, named event, temporal
   episode, identity quantity, stage, or actor/target discriminator for
   deterministic event-key proof;
2. retains hard-conflict vetoes ahead of exact-title and containment proof;
3. adds bounded production-derived aliases for event-defining actions,
   institutions, objects, locations, and actor direction;
4. treats earthquake magnitude as identity-defining;
5. preserves runner-up ambiguity rather than forcing a merge.

| Metric | Frozen result | Release floor |
|---|---:|---:|
| Candidate recall | 1.000 | ≥0.97 |
| Pairwise precision | 1.000 | ≥0.995 |
| Pairwise recall | 1.000 | ≥0.95 |
| B-cubed precision | 1.000 | ≥0.995 |
| B-cubed recall | 1.000 | ≥0.95 |
| Cluster purity | 1.000 | ≥0.995 |
| Hard-negative false merges | 0 | 0 |
| False splits | 0 | ≤1% of positive pairs |

The frozen distribution is 32 singleton clusters and 19 two-report clusters;
all 23 WorldMonitor-reference pairs pass. Reproduce with:

```bash
GMGN_TEST_POSTGRES_DSN=<isolated-test-dsn> \
  uv run pytest -q tests/integration/test_news_story_evaluation.py
```

This labeled pair corpus is necessary but not complete ground truth and does
not exhaust multi-member transitive shapes. Production cutover must therefore
also prove a verified backup receipt, material-fact preservation, sequential
Identity-v2 rebuild, zero backlog and hard-conflict violations, post-rebuild
distribution/candidate audit, both Story views, Active Brief hash closure,
Chinese provider or exact-cache provenance, and all five News health layers.
The pre-cut 0198 Stories are not considered corrected.

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
