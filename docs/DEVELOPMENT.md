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

### News WorldMonitor parity evaluation

The public News authority is WorldMonitor commit
`0e8785c43e6a693990a14181ae0a16066c15fc8c`. Exact differential parity begins
after Tracefold has adapted OpenNews into a canonical digest-like corpus with
keyword-sourced threats. It covers shared lexical Story identity, public
cluster scoring and selection, English L1/L2 synthesis, and whole-payload
`news:insights:v1` publication/LKG semantics. WorldMonitor's RSS population
builder, optional cached-classification branch, Dashboard ISQ reorder, and
personalized Digest Magazine pipeline are reference or excluded surfaces, not
Tracefold runtime contracts.

Tracefold retains one complete current 12-hour OpenNews Story closure. One
provider record becomes one NewsItem: the first logical plaintext block is the
bounded canonical headline, remaining text is bounded description evidence,
and reporting origin follows the verified-wrapper/news-type/host/OpenNews
precedence. Provider AI metadata remains descriptive and may qualify outbound
Push only; it cannot affect Story identity, public scoring, admission, ordering,
or Brief. The public seed applies the pinned JavaScript UTF-16
`title.length > 10` gate and reclusters eligible Items with the same identity
kernel before selection. The public selector is one global server-owned order with no profile,
preference, embedding, topic grouping, entity veto, private diversity rule, or
client-side reorder.

`tests/test_news_worldmonitor_parity.py` is the maintained local golden suite.
It covers positive and negative title pairs, exact-title and containment merges,
CJK features, hot buckets, order independence, classification, historical
exclusions, importance rounding, public selector ordering and admission,
corroborated-lead reservation, and the frozen reporting-origin tier-map digest.
`tests/test_news_pinned_worldmonitor_differential.py` is the actual upstream
differential: when the pinned sibling is present, it imports and executes its
identity, selector, prompt, parser, and composer modules and verifies the result
against the committed golden before comparing Tracefold. Portable runs without
the sibling use that same golden rather than skipping the lane.
The identity port also fixes the pinned Node runtime's Unicode 17 letter,
number, uppercase, and lowercase semantics so host Python Unicode data cannot
silently change Story IDs.
The real-PostgreSQL public pipeline suite additionally covers the canonical
OpenNews adapter, complete Story closure, singleton selection, L1/L2 composer,
immutable publication, and whole LKG decision. The retired RSS URL/membership
inventory is not a runtime or parity contract. Run the differential lane with:

```bash
TRACEFOLD_WORLDMONITOR_REPO=/path/to/worldmonitor \
  uv run pytest -q \
    tests/test_news_pinned_worldmonitor_differential.py \
    tests/test_news_worldmonitor_parity.py
```

The repository defaults to the sibling `../worldmonitor`. A present repo at any
commit other than `0e8785c43e6a693990a14181ae0a16066c15fc8c` fails. Release
evidence uses that exact sibling so the frozen-golden fallback is not mistaken
for a fresh upstream execution.

The release evaluation replays the current production 12-hour NewsItem corpus
in isolated PostgreSQL. Record the source cutoff; Item, Story,
singleton/multi-member, reporting-origin, and category counts; every materially
large cluster; highest-similarity non-merges; selected Top Story evidence and
drop distribution; and both pinned commits. This is a distribution and parity
review, not a compression target. The Story turn must remain inside the
10,000-row, 8 MiB, 25-second, and 60-second freshness boundaries without
sampling or widening the window.

Cutover acceptance requires one atomic hard cut: exactly thirteen News tables,
five public News routes, one enabled OpenNews runtime source, one acquisition
module, one fixed-period Story/selection writer, one native model seam, and no
old payload, prompt, selector, reading-layer localization, personalized, or
compatibility path. The primary maintained seam sends representative OpenNews
frames through real PostgreSQL, the production complete Story projection and public selector,
fake external model transports, immutable publication, and authenticated
`GET /api/news/brief`; repositories, transactions, calculation, selector,
composer, state machine, and HTTP serialization remain real.

Release evidence additionally proves canonical headline/origin normalization
and repeat-delivery zero writes; complete Story membership and CAS; exact public
Top Story order and provenance; L1 success or truthful L2/none degradation;
whole healthy LKG preservation without clock refresh or mixed snapshots;
bounded retries and fenced claims; no model/network I/O in a database
transaction; all five endpoints; generated contracts; and the responsive real
`/news/brief` route. The page keeps server order, makes Top Stories primary,
labels L1/L2 as an enhancement, preserves linkless evidence, and exposes no
publication history or personalized ranking.

When Push is enabled, acceptance still proves first-enable zero-send baseline
suppression, strict score greater than 70, the 15-minute Article deadline,
selected-Item ledger deduplication across Story-ID changes, frozen at-least-once
delivery, optional one-shot presentation-only title translation with immediate
original fallback, exact signed/unsigned Feishu shapes, bounded retry/terminal
classification, and no dependency on the serial model arbiter. Push state and
pending/retry rows survive the News hard cut; absence of a signing secret is an
explicit unsigned mode, never a fallback after a signed attempt fails.

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
