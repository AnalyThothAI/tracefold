# Architecture

Tracefold is a Python codebase with two sibling business capabilities, News and
Trading, a React operator console, and PostgreSQL persistence. Serve and Workers
share the application image. The optional Nautilus execution process has its own
image and lifecycle; it is not a third business context or a News worker.

This document maps the current owners, data flow, and boundaries. Exact public
fields belong to [Contracts](CONTRACTS.md) and [generated schemas](generated/README.md);
operator procedures belong to [Operations](OPERATIONS.md); coding and verification
policy belongs to [Development](DEVELOPMENT.md). Current source owns implementation
constants, enabled tasks, and state transitions. Historical Issue plans explain
past decisions but are not additional runtime or PR requirements.

## Data flow

```text
OpenNews Strategy WSS / history recovery
  -> RabbitMQ raw handoff -> News admission -> PostgreSQL Item
       |-- editorial: Event -> triage handoff -> News Program -> deterministic decision
       |      -> verdict + delivery intent -> sender -> durable delivery outcome
       |-- market: typed OI / liquidation / smart-money fact
              |-- market notification rules -> durable intent -> sender -> outcome
              `-- public OI projection -> App mapper -> Trading Source / Case / Signal

Tracked-wallet roster + chain receipts
  -> wallet fill ledger -> net-buy detector -> episode + first notification intent
  -> independent price observations

Trading Signal + authenticated OperatorIntent
  -> separate Nautilus Runtime -> venue / reconciliation -> ExecutionObservation

PostgreSQL read projections -> Serve -> HTTP / React console
                           `-> read-only CLI commands
```

The editorial and market paths deliberately diverge at admission. A market
measurement does not need an editorial Event, model verdict, learning cohort, or
reader card before it can be stored or reach the Trading source projection.
The current Trading policy consumes OI evidence; an arbitrary news explanation is
not itself an implemented automatic-trading strategy.

The canonical Compose application path includes PostgreSQL, RabbitMQ, the one-shot
broker-policy application, migration, Serve, and Workers. `make up` manages that
application lifecycle; the separate `make runtime-*` targets manage execution.
See [compose.yaml](../compose.yaml), [Makefile](../Makefile), and [Setup](SETUP.md).
Changing News or Serve is not permission to restart an account-owning runtime.

`tracefold init` generates operator defaults; `~/.tracefold/config.yaml` is the
application configuration authority. Missing optional provider credentials are a
capability state, not a reason to fabricate data. Inspect redacted configuration
and actual capability status rather than infer operation from task construction.

## Package map

Production code is under `tracefold/`, without a `src/` parent:

| Package | Responsibility |
| --- | --- |
| `tracefold.news` | Admission, editorial Events, Program, deterministic decisions, delivery, review/learning/release, market observations, wallet episodes, and `news_*` storage. |
| `tracefold.trading` | OI source admission, frozen Cases, Alpha evaluation, engine-neutral Signals, execution transport contracts, and `trading_*` storage. |
| `tracefold.integrations` | Provider, broker, delivery, public-market, and Nautilus/Binance adapters. |
| `tracefold.platform` | Configuration, PostgreSQL/Alembic, telemetry, identity, and bounded process resources. |
| `tracefold.app` | Serve/Workers/Nautilus composition, HTTP/CLI adapters, database-port implementations, and News → Trading mapping. |

The dependency direction is:

```text
app -> integrations + news + trading + platform
integrations -> the relevant business contracts + platform
news -> platform
trading -> platform
platform -> Python / third-party libraries
```

News and Trading neither import each other nor read the other's tables. Ordinary
cross-package consumers use the business package's public value and port contracts.
App composition and concrete integration collaborators use explicit internal owner
imports where the architecture harness permits them; they do not enlarge public
exports simply to construct an implementation. Package roots perform no runtime I/O.

`app/workers/wiring/news_to_trading.py` maps the public News OI projection to Trading's
own row contract field by field. The News read finishes before the Trading transaction
starts. There is no callback holding both repositories and no cross-context transaction.
A change to editorial policy or Program identity does not by itself change this OI
projection contract.

`tests/architecture/test_backend_boundaries.py` enforces the implemented dependency
and SQL boundaries. Installed-distribution tests verify the wheel outside the checkout;
a local import from the repository alone does not establish correct packaging.

## Truth, control state, and derived state

| Kind | Examples | Meaning |
| --- | --- | --- |
| Source facts | Items, typed market observations, wallet fills and source provenance | What was observed and durably recorded, including explicit uncertainty. |
| Decisions and receipts | Verdicts, accepted reviews, Signals, delivery outcomes, execution observations | What the named owner decided or actually observed, with its evidence and identity. |
| Control state | Claims, retry scheduling, broker queues, runtime/capability status, release activation | Progress and authority, not an alternative copy of the business facts. |
| Derived views | Event membership, quotes, reactions, current episode state, HTTP/React projections | Replaceable projections with an identified writer and freshness meaning. |

A model prediction is not a source fact. An accepted review is a recorded acceptance,
not proof of independent human accuracy. A queued notification is not a delivery;
a recorded command is not an accepted order or fill. Reconcile uncertain external
writes with their provider or venue rather than infer success from a local intent.

Current projections use stable business keys and identified writers. Preserve their
fact lineage and avoid rewriting unchanged business payloads. Provider timestamps,
host availability timestamps, and database timestamps describe different clocks;
do not invent ordering between independent clocks or clamp a measured source time.

News learning artifacts bind the actual Program, execution envelope, policy, review,
and dataset identities as provenance. Review and dataset eligibility follow the
evidence snapshot and the accepted labels, not the runtime bundle; runtime bundle
changes name a cohort for release evidence only. Retired hand-written epoch numbers
are audit history. Read the owning identity and release code when changing those contracts.

## Transaction ownership

The caller owns the transaction; repositories use its supplied connection and do
not hide commits. Business database callbacks receive only their bounded repository
capability, not a cross-context session or an escape hatch into App internals.

Important atomic units include an admitted Item plus its editorial assignment or
typed market fact; a verdict plus its delivery intent; a Case plus its admission
record; a Signal plus its Case transition; and a complete wallet receipt's facts
or derivation updates. Read the owning repository for the exact predicates.

Provider, model, broker, filesystem, and other external I/O runs outside database
transactions. Prepare expensive validation, canonical serialization, and hashes
before the callback; materialize richer objects after it. Keep SQL, locks, row mapping,
and immediate conditional-write checks bounded inside the transaction.

Database and finite-operation adapters own native deadlines and physical resource
permits. An asyncio timeout must not imply that an underlying operation has stopped
or release its permit early. Preserve cancellation, completion, and retry semantics
at the actual adapter boundary, rather than copying another timeout wrapper into a
business loop. [Operations](OPERATIONS.md) and the relevant tests cover diagnosis.

## External Data runtime contract

External data is not a generic shared scheduler or an extra business capability.
Different flows have different replay and loss semantics. The Workers stage annotations
and architecture tests currently distinguish:

| Class | Meaning |
| --- | --- |
| `durable_event` | Every admitted event matters; persist and recover idempotently. |
| `latest_state` | The newest useful value matters; coalesce refresh work and retain explicit stale/unavailable state. |
| `derived_work` | Bounded work can be rebuilt from durable facts and provider history. |
| `signal_truth` | The Trading lane commits its durable engine-neutral decision and Case transition. |

These annotations describe Workers business stages; they do not select providers or
schedule tasks. External account/order authority belongs to the separate Runtime.
It is not another `work_semantics` value to add to a News collector, and uncertain
orders must not inherit an ordinary quote-refresh retry policy.

### Canonical inventory

Use the current composition rather than a hand-maintained table of provider counts,
model names, refresh intervals, and historical tasks:

| Flow | Current owner |
| --- | --- |
| OpenNews admission and editorial processing | `tracefold/news/pipeline/` and App News wiring |
| Instruments, current quotes, Event reactions | News market-review owners and their provider adapters |
| OI, liquidation, smart-money notifications | `tracefold/news/market_notifications.py` |
| Wallet receipts, net-buy detection, price sampling | App chain-tape wiring and its three independently supervised task declarations |
| OI Source → Case → Signal | `tracefold/trading/signal_lane.py` |
| Account, orders, protection, reconciliation | Nautilus integration, composed by `tracefold/app/nautilus/` |

The code-owned limits still apply. Inspect their definitions and consumer tests when
changing cadence or budgets; this map intentionally does not keep a second numerical
configuration ledger.

### Extension and extraction gates

For a new flow, identify its authority, meaning, consumers, persistence/recovery needs,
bounded I/O, and failure behavior. That design can be part of the implementing Issue
or PR. Do not require a separate form for each item or an arbitrary number of providers
before extracting a useful shared component. Equally, do not introduce a registry,
base-worker hierarchy, or generic retry engine without an actual common responsibility.

## Workers task set

`tracefold/app/workers/task_contract.py` is the authoritative declaration of task
names, capabilities, and whether a failure is foundational. App owns polling,
cancellation, and supervision; business runners own their action and durable state.

Reception, recovery, admission, and retention are foundational News tasks. Optional
capabilities include editorial judgment, delivery, instrument/quote/reaction review,
market notifications, the wallet tasks, and the Trading Signal lane when configured.
The root also owns the probe and singleton/control work.

The wallet composition currently declares `news-chain-tape`, `news-wallet-net-buy`,
and, when available, `news-wallet-prices`. There is no current wallet digest or
single-wallet research task. A declared task is not proof that its capability is
available or healthy; read composition status and actual durable progress.

Unexpected errors in optional tasks are attributed to their capability while healthy
siblings can continue. Foundational failures and shared infrastructure/ownership
failures retain their root-level semantics. Do not turn one missing optional provider
into a blanket denial of healthy read APIs, or hide a required ingestion failure
behind a green readiness response.

## Product flows

### News

Admission stores normalized provider Items and separates editorial from market input.
Editorial Items join same-kind Events through the existing dedupe and grounding owners.
Triage receives frozen evidence rather than mutable provider responses.

The public semantic seam is `SemanticJudge.judge(TriageContext) -> SemanticJudgment`.
The native DSPy Program executes EventSemantics, Taxonomy, and ReaderCard predictors;
deterministic assembly and policy own validation and the reader-facing decision.
A better-looking model answer alone does not establish a better final notification.

The released Program image is the native DSPy state document
(`news_program_state_v1`: instructions, demos and Signature state per predictor, no
model routes) loaded through one path; routes come from operator configuration. A
learning run names one target — `classification` (Taxonomy), `understanding`
(EventSemantics) or `explanation` (ReaderCard) — and GEPA moves only that predictor's
state, on that predictor's own production primary endpoint. Assets carry a typed
market identity `(market_type, symbol, role)`; Taxonomy may fail on its own
(`taxonomy_status=unavailable`) while the code-owned `source_authority` on the
editorial envelope and the reader card survive. [News taxonomy](NEWS_TAXONOMY.md)
owns classification language; program and learning code own signatures, budgets,
identity, metrics, and selection.

Review proposals, explicit acceptance, frozen datasets, optimization, candidate
registration, evaluation, and production release are distinct actions. An optimizer
cannot make its own proposals accepted truth or authorize its own promotion. Use
[CONTEXT.md](../CONTEXT.md), the current CLI, and [Operations](OPERATIONS.md), rather
than old experiment transcripts or machine-specific model presets as instructions.

An approved editorial decision and delivery intent commit together. The sender claims
work, performs the provider call outside the transaction, and persists the outcome.
Only an actual sent receipt means the reader received a card. Retry is conditioned on
what the provider failure proves; an unknown result is not proof of non-delivery.
Price or contextual presentation reads must not silently become a second decision
policy or require a card to wait indefinitely.

#### Price Review plane (#88, #304)

News market review owns latest quote snapshots and versioned Event reactions.
Quotes are current display state, not a tick-history ledger; reactions compare an
Event anchor with a defined observation horizon and resolve a price contract by the
judgment's typed market identity (`reaction_v2`): an equity subject measures against an
equity contract or nothing, never the same-name coin. Their interpretation, source choice,
coverage, and missing-data behavior belong to the owning versioned implementation.
The independently bounded Workers loops perform provider I/O without holding a
transaction and publish their derived views without changing editorial admission.

### Why the quote source is REST and not a WebSocket

The current News quote/reaction plane is bounded review and presentation work, not an
order-book or tick-history trading feed. It uses public REST adapters and latest-state
or historical-window semantics. Quotes are not another editorial truth source.
A WebSocket may be appropriate for a different product requirement, but it must be
justified by that consumer's latency and loss/recovery needs, not added because a
historical manual declared one transport universally faster or permanently forbidden.

## Trading core

`tracefold.trading` is disabled by default and owns Source → Case → Signal, not account
execution. Its implementation is separate from News classification and reader delivery.

### The domain language

| Term | Meaning |
| --- | --- |
| Source | An OI observation with provenance and an admissible source contract. |
| Case | Frozen point-in-time source, market, and policy evidence for an Alpha decision. |
| Signal | An engine-neutral decision; not an account, size, leverage, or order instruction. |
| OperatorIntent | An authenticated durable control request, not proof of Runtime acceptance. |
| ExecutionObservation | A recorded Runtime/venue outcome, not a promised future fill. |

### The one live path

```text
public News OI projection -> explicit App mapping -> Trading admission
  -> frozen Case -> pure Alpha evaluation
  -> NO_TRADE on the Case, or atomic SIGNAL_EMITTED + TradeSignalV1
  -> separate Runtime authority -> order / fill / protection / exit observations
```

The handoff reads the OI ledger and Item provenance, not editorial verdicts, Events,
Program identity, or learning epochs. Trading does not use RabbitMQ as an execution
queue. The current Alpha is long-only; extending strategies is a product/code change,
not something a news explanation or architecture diagram already implements.

### Admission

Admission owns source validation, supported venues, freshness, market context,
liquidity, and source idempotency. Rejections and deferred work remain explainable
through the admission ledger. `sources.py` owns the supported source vocabulary;
`admission.py` owns current admission semantics. Do not maintain a second source or
venue-priority registry in documentation or App wiring.

### The Case and its manifest

A Case freezes source identity, cutoff, market context, and exact policy configuration.
The policy runs against that frozen evidence. A successful signal insert and its
Case transition are atomic. Account/risk sizing and executable venue routing do not
belong in the Signal lane or its manifest as shadow Runtime state.

### Runtime ownership

The Nautilus integration owns account state, execution routing, risk, orders,
protection, exits, and reconciliation. App supplies its process/database/probe
composition. Paper and live use the configured Runtime path with their respective
account/environment settings; neither mode turns a local command into a fill.
Inspect the pinned dependency and Runtime construction for implementation details.

The business Signal lane imports no Nautilus engine and has no order authority.
This is a real separation of responsibilities, not a claim that the repository has
removed the Nautilus integration entirely.

### Failure semantics

A business refusal is different from a storage, process, or venue failure. Preserve
transaction rollback and replayability instead of marking an input consumed because
infrastructure failed. Unknown external order results require reconciliation, not
blind resubmission. Recorded intent, Runtime acceptance, order acceptance, fill, and
venue-proven flatness are separate facts in CLI, HTTP, and React views.

### OI research replay

Offline OI corpus/replay research lives in [notebooks](../notebooks/README.md), outside
the service's runtime imports and account authority. A backtest, local replay, or
code test is not a live execution receipt or production profitability proof.

### Runtime and cutover

The execution process has a separately built image and explicit `make runtime-*`
commands. Application deployment must not implicitly restart it. Follow
[Operations](OPERATIONS.md), [Security](SECURITY.md), and current readiness/account
state for an authorized cutover. Disabled execution does not require live credentials.
Do not infer a safe rollback from an old Issue receipt or a green unit test while a
live account may still have exposure.

## Market observations (#137, #553)

Market frames and editorial Events answer different questions. Admission stores a
market Item and its typed fact without running editorial dedupe, Gate, or Triage.
Unknown or unparsable evidence remains visible with its raw data and parse reason;
it must not be converted into a fabricated measurement.

### What is stored

Items retain provider provenance and parsing state. Typed OI, liquidation,
smart-money, and derived wallet observations have their own identities and consumers.
OI publication time comes from the Item/source fact, not a required editorial Event
that this path does not create. Retain independent observed/available/persisted clocks.

### The parsers, and what is no longer beside them

The owning parser translates an identified source contract into typed facts.
Unmatched templates are explicit raw/failed-parse states. A new market observation
does not become a duplicate merely because its symbol matches an earlier measurement.
Parser, notification, and Trading admission decisions are separate responsibilities.

### How it is read

Market list/detail APIs read the persisted market projection. Parsing status and
notification status remain separate: a parsed fact may legitimately be unsent.
Exact fields and pagination belong to [Contracts](CONTRACTS.md), not an inferred
editorial Event or copied frontend schema.

### The market notification loop

The market notification owner applies direct rules to durable observations, creates
intents, and uses the shared delivery outcome semantics. Bounded display quote/context
reads are optional presentation inputs, not prerequisites for fact admission or a
second market policy. An unknown send is neither a delivered receipt nor permission
to retry blindly. Optional notification failure must not erase the underlying facts.

### The wallet tape (#572 PR-1)

The tracked-trader provider supplies roster context; chain receipts supply fills.
The tape records transaction/log identities, raw quantities, cash attribution, and
roster provenance. Plain transfers are not automatically buys, and missing cash
attribution is unknown pricing, not zero spend. Overlap and idempotency support
re-reading, but a stored block hash alone does not implement full reorg repair or
prove complete historic position coverage.

### Concentrated wallet net-buy episodes (#641)

The three independent tasks collect receipts, detect concentrated net-buy episodes,
and sample prices. Detection and first notification have no balance, bags, external
quote, or model dependency. Complete transaction facts and derivation progress commit
atomically. The detector calculates the supported windows from the same fill set,
with explicit member coverage, pricing, and exclusion reasons.

An episode retains an immutable first snapshot and an independently updated current
snapshot. Its logical first notification uses the existing market intent/delivery
owner. Before the first attempt, eligibility and freshness are checked against actual
persisted state; the attempted payload then freezes. Price samples remain independent
observations with target and actual times. Without a known trigger baseline, returns
remain unknown rather than invented.

The console reads `/api/news/wallets/events` and episode detail; the roster endpoint
is auxiliary context. The old card API, single-wallet research, exit/crowding rules,
and digest tasks are not the current product. See the
[wallet cutover runbook](wallet-net-buy-cutover.md) for migration and validation.

### Retention

Apply the owning retention policy to source facts, projections, receipts, and learning
evidence according to their actual lifetime and foreign-key lineage. Market facts do
not gain editorial-review evidence merely by sharing `news_items`. Preserve required
audit and active release references; use current schema and retention code, not a
manually counted table list or obsolete migration narrative.
