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

Business capabilities are exported from `tracefold.market`, `tracefold.news`,
and `tracefold.macro`. Code outside the owning package imports only those
roots. Keep internal modules cohesive and move behavior behind the root
interface instead of adding forwarding modules, aliases, or compatibility
packages.

Private application composition and concrete provider adapters may use only
the exact package-private implementation seams named in the architecture
harness—for example, to construct an owned repository or reuse a pinned parser
behind a public protocol. This is implementation wiring, not a caller-facing
interface: public models/protocols still come from the package root, and new
private import edges fail until deliberately reviewed and enumerated.

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

### Operator onboarding acceptance

Startup changes treat `make up`, `make status`, `make logs`, and `make down` as
one public lifecycle seam. Tests must keep `tracefold init` as the only default
config authority, reject a returned static example, verify `0700`/`0600`
permissions and non-rotation, inspect the Compose role/bootstrap boundary, and
prove that status cannot pass with a failed migration, stopped Worker, failed
readiness endpoint, or missing console.

The release acceptance uses an isolated empty home and a distinct empty Compose
volume. The first `make up` must reach Alembic head with healthy Serve/Workers
and real console HTML; the second must preserve config/password hashes and a
database sentinel, leave the PostgreSQL container identity and start time
unchanged, and run migration, Serve, and Workers from one image whose runtime
and OCI revisions match the checked-out commit. Missing optional live
credentials must appear as capability degradation rather than startup failure
or fake data. A failed startup keeps its containers/logs available for
`make logs` and returns non-zero.

The focused maintained checks include:

```bash
uv run pytest -q \
  tests/architecture/test_onboarding_surface.py \
  tests/integration/test_cli.py \
  tests/integration/test_compose_postgres.py
```

### Test lanes and speed

`make test` is the default regression (unit + architecture + contract +
integration, excluding `slow`, `e2e`, and `golden`; ~4 minutes on an idle
machine with the local test PostgreSQL). `make test-slow` runs the real-process
Workers runtime tests (`tests/integration/test_workers_runtime_v2.py`, bounded
by wall-clock deadlines, ~2 minutes), `make test-e2e`/`make test-golden` the
service and corpus lanes, and `make test-all` everything (~6.5 minutes).
Integration tests reset the schema per test through `prepare_postgres_database`
only when they seed data; validation/auth-only API tests reuse the migrated
head. Historical migration tests exist only for lanes that still ship; retired
lanes keep no migration tests.

### News V3 evaluation seams

News V3 is evaluated through three public seams: `tracefold.news.eval.replay`
(`tracefold news replay <hits.json>` runs Deduper+Gate in memory over provider
hits; `tests/fixtures/news_v3_hits_sample.json` is the real, redacted golden
corpus), `tracefold.news.triage_rules.decide()` (pure post-rules, golden-tested
in `tests/news/test_news_v3_pure.py`), and `tracefold.news.analyst_rules.
verify_verdict()`. Broker behavior is covered by
`tests/integration/test_news_bus_rabbitmq.py` against the compose RabbitMQ
(`TRACEFOLD_TEST_AMQP_URL`, default `amqp://tracefold:tracefold@127.0.0.1:5672/`;
skipped when unreachable). Run the focused lane with:

```bash
uv run pytest -q tests/news tests/integration/test_news_v3_pipeline.py \
  tests/integration/test_news_v3_consumers.py tests/integration/test_news_bus_rabbitmq.py
```

Before the hard cut, run a read-only shadow against the current 96-hour RSS and
12-hour OpenNews fact population and attach the report to Issue #44. Record
physical Items, exact atoms, Stories, size P50/P90/P99/max, current-to-V2
merge/split counts, candidate/accepted/rejected/conflict/ambiguity/grounded
counts, event-family distribution, encoded input, compute time, and every Story
larger than 20 Items with a manual coherence disposition. The projection must
stay within 10,000 Items, 8 MiB, 250,000 candidate pairs, 25 seconds, and 8 KiB
per Story evidence; do not repair failures through sampling or dynamic windows.
After cutover, attach Story distribution, analyzed query plans, News health,
first coherent V2 selection/Brief evidence, and an unchanged-publication
zero-write result. Two clean full generation/check runs must have no drift.

Use the configured Serve role so PostgreSQL enforces the zero-write boundary:

```bash
uv run tracefold config
uv run python scripts/news_story_v2_shadow.py
```

The JSON report includes the database revision, version fingerprint, current→V2
merge/split comparison, deterministic decision counters, bounds, and an explicit
manual-review list for every V2 Story larger than 20 Items. It never emits
provider credentials, raw provider metadata, comparison tokens, or rejected
titles.

`20260813_0266` historical cutover acceptance requires one atomic hard cut: exactly ten
News tables, five public News routes, and one zero-send authenticated OpenNews
WSS consuming automatic account `strategy.triggered` pushes for the exact
two-ID allowlist `1018`/`1019`, the exact
opt-in 179-feed public RSS breadth/corroboration catalog, one acquisition module,
one dirty-triggered Story/selection writer, one native model seam, and no ordinary
`news.subscribe`, `/open/news_search` recovery, dual writer, old Push policy,
personalized, or compatibility path. The primary maintained seam sends
representative RSS responses plus configured NEWS and MARKET/OI Strategy
frames through real PostgreSQL, the production complete Story projection and
public selector, fake external model transports, the half-hour
frozen-slot/current-LKG state machine, and authenticated `GET /api/news/brief`;
repositories, transactions, calculation, selector, composer, and HTTP
serialization remain real.

Release evidence additionally proves the socket sends no application
subscribe/request during connect while retaining literal ping/pong and RFC
control heartbeat, the first automatic trigger is not consumed as an ACK,
configured Strategy admission ignores `engineType`, canonical headline/origin
normalization, exact replay zero writes, same-event sorted-unique multi-Strategy
provenance union, and distinct-event fact identity; complete Story membership
and CAS; exact public
Top Story order and provenance; L1 success or truthful L2/none degradation;
whole healthy LKG preservation without clock refresh or mixed snapshots;
the exact Ollama -> configured direct DeepSeek -> Groq order; all-or-none
direct endpoint/key/model validation; bounded retries and fenced claims; no model/network I/O in a database
transaction; all five endpoints; generated contracts; and the responsive real
`/news/brief` route. The page keeps server order, makes Top Stories primary,
labels L1/L2 as an enhancement, preserves linkless evidence, and exposes no
publication history or personalized ranking.

Historical `20260814_0270` acceptance proves one fail-soft effective-availability
epoch, same-transaction first-live Item/outbox creation, recovery/RSS/pre-epoch
exclusion, immutable source snapshots, and two independent Push attempts for
two provider Item IDs even when Story later merges them. It proves optional
one-shot title-only translation with immediate original fallback, translation
outside database transactions, exact signed/unsigned Feishu shapes, and one
Feishu attempt ending in `sent` or `terminal` with no retry, lease, or reaper.
Startup terminalizes interrupted `sending` rows before acquisition. The hard cut
terminalizes incompatible legacy unsent Story-policy rows while preserving
completed legacy card audit and the Push enablement fence;
absence of a signing secret is an explicit unsigned mode, never a fallback
after a signed attempt fails.

Historical `20260815_0271` acceptance proves one durable exact-title presentation
decision shared by Feed/detail and Push, with no history backfill or
compatibility reader/writer. Every newly accepted OpenNews, recovery, or RSS
title creates its intent atomically; current live Push-blocking work outranks
the FIFO remainder. Chinese/oversized input bypasses providers. Other titles
make at most one call with the active DeepL key, then at most one direct
DeepSeek call, then settle the original. Permanent DeepL authentication/quota
failure rotates only future Items; transient failure retains the key and the
current Item never tries another DeepL key. Tests prove exact fingerprint
matching, original-title search/Story/Brief semantics, provider deadlines,
fallback visibility, startup reconciliation, and fatal Workers termination if
a fenced external outcome cannot be settled. The hard cut adds the eleventh
News table, terminalizes pre-cut nonterminal Push, preserves renamed legacy
presentation JSON as audit only, and performs no migration-time provider or
outbound call.

Current `20260815_0273` acceptance is Issue #45 and is exercised at the existing
OpenNews Item transaction plus real PostgreSQL/worker seam. Tests prove that
three distinct provider IDs for one exact atom create three Items and title
presentations but only one Feishu call; normalization-only variants suppress;
numbers, percentages, amounts, and similar non-exact Story members do not;
provider-time window boundaries, cross-transaction leaders, terminal leaders,
replay, rollback, and batch permutation are deterministic; and suppressed rows
never enter sender or restart recovery. Story V2 golden/permutation tests must
stay byte-for-byte unchanged through the public `build_story_projection` seam.
Schema tests cover the composite compatible-leader FK, counter invariant,
partial leader/recent-suppression indexes, no new News table, and the 0273
offline hard cut. Contract/frontend tests cover versioned aggregate status and
bounded title-free evidence. Run the focused suites, `make check`, contract
generation twice, and the selected backend/frontend gates before cutover.

Production cutover review records exactly `1018` (News Score > 70) and `1019`
(OI Event Monitor), a redacted configured count of `2`, one redacted real
MARKET/OI frame and one redacted NEWS frame, the deterministic provenance-union
result for a same-event multi-Strategy match, and explicit confirmation that a
scoreless MARKET/OI live event creates one Item Push independently of whether
Story projection later succeeds.
Listing and Delisting Announcements and Storage News may remain enabled
provider-side but are explicitly outside this cutover. Any future addition is a
reviewed configuration change, never implicit provider-side enablement.

Transport/status acceptance records disconnect, overflow, process outage, and
planned shutdown with distinct causes. Database backpressure must retain WSS;
overflow records an incident without falsely disconnecting it. Reconnect
restores current state independently of bounded official Strategy list/hits
recovery. Tests prove overlap idempotency, complete/partial/unavailable status,
and that Search never appears in the production recovery seam.

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
