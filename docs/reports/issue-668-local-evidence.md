# Issue 668: local evidence engineering receipt

Scope: [#668](https://github.com/AnalyThothAI/tracefold/issues/668), including its
consolidated #669/#670 responsibilities and cancellation of #671 web fetching.
Base: `449ee87ad4d3614e7b8c3228a1731b3e3ae92f16`; refreshed against origin/main
before delivery. This receipt covers implementation and finite engineering checks,
not deployment, accepted Gold, paid model experiments or production quality claims.

## Delivered behavior

- Removed the webpage client, reader interface, cache reads/writes, enable setting,
  late-document detail query, obsolete single-item material reader and nine exclusive
  dependency packages. Shared HTTP and finite-operation resources retain their consumers.
- Passed minimal members from the exact frozen Event snapshot. Preparation fixes one
  cutoff, caps candidates at 16, reads metadata once, deduplicates complete-body hashes,
  and loads at most four bodies in one batch. Late/missing bodies use frozen facts.
  Same URL/source/summary cannot erase a distinct correction body.
- Selected focus sentences before unrelated opening prose and local qualifications
  before filler; retained exact offsets, hashes, amounts, decimals, negation and
  attribution. Deterministic sentence deduplication preserves conflicting states.
  Current shares 6,000 characters/12 spans; related shares 2,400/24 across four materials.
- Applied subject/event relevance and known typed identity conflict checks across all
  candidate channels. Reused the existing tokenizer with mixed-script separation,
  short asset symbols and CJK bigrams. Returned candidate limits do not claim bounded
  database scans. Source links are still local identifiers, never network requests.
- Kept three Predictors and ReaderCard's three fields. Seed and native state preserve
  amounts, units, flow direction, approval conditions and attribution, including the
  split/affordability counterexample. Legal refs only attest citation linkage; empty
  refs are visible diagnostics. Semantic counterexamples are diagnostic fixtures,
  not a new validator, judge, rejection policy or accepted Gold.
- Cut the online input to v2, Program to v12 and envelope to v7. Historical document
  fields decode only in archived execution views. Reanalysis adapts frozen previews
  with explicit provenance and never fetches today's material as historical input.
- Preserved 0384 and historical rows. Forward-only 0385 admits v12 to the existing
  judgment CHECK and adds the query-specific title GiST index. A real v11 verdict
  and webpage archive survive the migration byte-for-byte; fresh/head-no-op and
  downgrade-refusal paths are tested.

## Verification

Test resources were private databases on a disposable PostgreSQL 18.4 container;
no production database, broker, model endpoint or notification destination was used.

- `make check-static`: passed (Ruff, formatting, mypy, generated/router/link checks,
  compilation).
- `make test-fast`: **2,741 passed**, 882 deliberately deselected by the hermetic lane.
- `TRACEFOLD_TEST_RESOURCES_REQUIRED=1 uv run pytest` with the following files:
  **130 passed**: `test_news_evidence_material.py`, `test_news_v3_consumers.py`,
  `test_news_crash_replay.py`, `test_news_v3_pipeline.py`,
  `test_news_reader_card_fidelity.py`, `test_news_reader_history.py`,
  `test_migration_history.py`, `test_postgres_schema_runtime.py`, all under
  `tests/integration/`. These exercise real production preparation/SQL, bounded
  batch counts, CAS/stale re-ask, sent-only history, send binding, read-only details
  and old-row migration preservation.
- `uv run pytest tests/contract/test_openapi_codegen.py -q`: **1 passed**.
- `npm --prefix web run typecheck`, `run lint`, `run build`: passed.
  `run test:unit`: **245 passed**; lint's architecture suite: **23 passed**.
- Native state regenerated through `regenerate_stable_program_state`; OpenAPI and
  TypeScript through the owning generators. The schema generator produced no table
  column changes. No Trading implementation or published 0384 migration changed.

## Query-plan evidence and limits

Reused the existing synthetic harness, with an output-path override:

```bash
PYTHONPATH=. TRACEFOLD_TEST_POSTGRES_DSN=<disposable tracefold_test database> \
TRACEFOLD_RETRIEVAL_REPORT=docs/reports/issue-668-retrieval-plan.json \
uv run python docs/reports/issue-664-retrieval-reproduce.py
```

[Full EXPLAIN and timings](issue-668-retrieval-plan.json) cover 25,001 synthetic
Items/Events over 26 days, 1% matching titles and deliberately shared BTC asset noise.
There were 32 merged candidates in 25 timing samples; p50 was 137.3ms and p95
143.8ms on this local run. The pre-index plan filtered 6,401 recent Events in the
similarity channel; the final plan uses `ix_news_events_evidence_title` and reads
102 index rows there. The entity channel still inspects 6,400 time-index rows.
Only raw channel intermediates (64), channel outputs (8/24/32), merged candidates
(64), loaded bodies and model inputs are count-bounded. These are synthetic local
observations, not a production SLO, full-scale scan bound or demonstrated quality gain.
The new index adds storage and write/index-maintenance cost for Event title updates;
production index-build time and cost remain unmeasured.

Full hosted CI, deployment/e2e lanes and paid model comparisons are not claimed by
these local checks. No model-call budget was supplied, so semantic quality remains
unverified by live-model experiment. The deployed image/configuration is unchanged;
remove the retired webpage setting before starting the new image, and apply 0385
through the existing stopped-writer migration procedure.
