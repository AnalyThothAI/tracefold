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
              `-- News trade-event outbox -> App relay -> Trading Trigger / Case

Editorial verdict -> same News trade-event outbox, independent of reader-card delivery
Trading Analysis process -> bounded market data -> frozen evidence -> Agent assessment
  -> pure Decision -> optional TradeSignalV2 -> Nautilus final validity check
  -> scoped TradePlan -> order / fill / protection / exit observations

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
Trading analyzes only one verified target asset per source fact. The Agent sees
a finite candidate menu; source text cannot provide order authority.

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
| `tracefold.trading` | Target, evidence, assessment and decision contracts, frozen Cases, scoped Signals, execution transport contracts, and `trading_*` storage. |
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

`news/learning/target_metrics.py` owns every comparison between a Program answer and an
accepted review: the three per-target rulers, the typed asset and taxonomy comparisons the
composite metric also reports, and the outcome vocabulary a denominator is stated in. The
metric judge (`news/learning/judge.py`) belongs to the metric, never to the Program, and
cannot change `program_sha256`. A caller passes it in; nothing reads it from ambient state.

`news_trade_events` is committed with each News OI fact or editorial verdict.
The separate Analysis process selects a target through News' public instrument
projection, commits an idempotent Trading Trigger and initial Case, then confirms
the News outbox row. A crash between those commits repeats safely. Neither domain
imports or queries the other's internal tables.

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
| Wallet roster, receipts, net-buy detection, price sampling | App chain-tape wiring and its four independently supervised task declarations |
| News/OI Trigger → Case → assessment → Decision | `tracefold/app/trading_analysis.py` and `tracefold/trading/engine/` |
| Claim attempt and physical LM receipts, event WATCH, shadow net evaluation | Trading ledgers in `tracefold/trading/storage/analysis.py`; App performs bounded market/model I/O outside transactions |
| Account, orders, protection, reconciliation | Nautilus (the Cache, reconciled with the venue), driven by the Runtime Strategy composed in `tracefold/app/nautilus/` |

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
market notifications and the wallet tasks. Analysis has its own process and its
own market/Agent budgets.
The root also owns the probe and singleton/control work.

The enabled wallet composition declares `news-wallet-roster`, `news-chain-tape`,
`news-wallet-net-buy` and `news-wallet-prices`. There is no current wallet digest or
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

Grounding is the provider's name resolution plus two code conditions, and the Gate keeps
no name table of its own: a B+/A/A+ coin tag or a literal `$TICKER` is the grounded asset.
The conditions narrow it where a tag is about a word rather than an instrument — crude
needs the storyline registry's energy context, and every other commodity underlying needs
its own name in the text, bilingually (`events/grounding.py`). Beside that, each verdict
records how the Event carries every instrument it names (`cashtag`, `text`, `alias`,
`provider_tag`, `unsupported`) and what the catalogue holds for it. That reading is
recorded evidence for a later decision and for the console; `decide()` consumes none of
it. It is a signal rather than a rule because a symbol is not a name: a measured day of
delivered cards has correct primaries the text never spells (`LMT` for Lockheed Martin,
`0700.HK` for 腾讯) and mis-resolved ones it spells perfectly, so separating them needs an
issuer-name source the catalogue does not yet have.

The public semantic seam is `SemanticJudge.judge(TriageContext) -> SemanticJudgment`.
The native DSPy Program executes EventSemantics, Taxonomy, and ReaderCard predictors;
deterministic assembly and policy own validation and the reader-facing decision.
A better-looking model answer alone does not establish a better final notification.
EventSemantics outputs what a reader of the text can check and nothing about the
reader: typed assets, novelty and the ledger entry a restatement cites, direction,
scope, `fact_kind` — one of ten kinds of new thing a text can state — and the
`evidence_ref` it was read off. Every threshold is `decide()`'s, where one ordered
decision table turns those observations, the four taxonomy axes, the code-owned
source authority, the count of independent member texts and the told ledger into one
named action. Novelty is the model's claim and the action is the code's: a card the
model calls a restatement of a ledger entry it was shown is dropped whichever
direction it reports, because a reported direction is a reading rather than a fact,
while a real reversal arrives as a progression or new fact and keeps its duplicate
and budget exemptions.

The released Program image is the native DSPy state document
(`news_program_state_v1`: instructions, demos and Signature state per predictor, no
model routes) loaded through one path; routes come from operator configuration. A
learning run names one target — `classification` (Taxonomy), `understanding`
(EventSemantics) or `explanation` (ReaderCard) — and GEPA moves only that predictor's
state, on that predictor's own production primary endpoint. Assets carry a typed
market identity `(market_type, symbol, role)`; Taxonomy may fail on its own
(`taxonomy_status=unavailable`) while the code-owned `source_authority` on the
editorial envelope and the reader card survive. A storyline key is recomputed only
for the judgment that is being produced: the told and receipt ledgers a replay reads
carry the key their own delivery recorded, because a verdict written before the key
carried a market would recompute into a key the ledger never held.
[News taxonomy](NEWS_TAXONOMY.md)
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

`tracefold.trading` is disabled by default and owns Trigger → Case → Decision →
Signal, not account execution. Its implementation is separate from News
classification and reader delivery.

### The domain language

| Term | Meaning |
| --- | --- |
| Trigger | One accepted News catalyst or OI fact with source identity, revision and target selection. |
| Case | One fenced work item and its frozen source, target, market evidence, assessment and decision. |
| Decision | Pure compilation of an Agent assessment and finite candidate menu, including NO_TRADE and WATCH. |
| SignalV2 | A time-bounded, scoped entry suggestion with side, exit plan and price envelope; not an order or capital grant. |
| OperatorIntent | An authenticated durable control request, not proof of Runtime acceptance. |
| ExecutionObservation | A recorded Runtime/venue outcome, not a promised future fill. |

### The one live path

```text
News OI fact or editorial verdict -> durable trade-event outbox
  -> App relay -> Trading Trigger + initial Case
  -> per-asset fenced claim -> bounded MarketDataPort reads -> frozen evidence
  -> one structured Agent call -> pure Decision
  -> NO_TRADE / WATCH / shadow TRADE, or atomic published TradeSignalV2
  -> separate Nautilus Runtime -> final validity check -> scoped TradePlan
  -> venue order / fill / protection / exit observations
```

The default Agent policy is shadow only (`publish_signals: false`). Missing model
configuration, incomplete evidence, invalid model output and an active NO_TRADE
have distinct Case statuses. Long and short candidates share the same pure
compiler. Neither News delivery nor RabbitMQ is an execution queue.

### Admission

News freezes source provenance in its outbox. The App maps its public asset and
instrument projection into Trading's versioned economic identity. Only one
eligible primary asset can proceed; exclusions and unresolved native units
terminate by name while retaining the input denominator. An accepted Trigger
creates exactly one initial Case; a bounded WATCH may create a child Case with
the same `entry_scope_id`. Per-asset advisory locks coordinate fact handoff,
publication and the final Trading validity read. Claims use a fresh token and
lease; late Agent responses cannot commit.

### The Case and its manifest

A Case freezes source identity, target mapping, knowledge cutoff, raw market
results, feature values, brief, assessment and policy decision as content-addressed
references. The market adapter shares closed bars and records physical request
receipts; it does not decide trade eligibility. A successful SignalV2 insert,
decision and Case transition are atomic. Source corrections/revocations and
the Runtime's final validity check can prevent a later order without rewriting
the frozen recommendation. Account risk, sizing and venue orders stay in Nautilus.

### Runtime ownership

Nautilus owns execution state (#680). Each fact has exactly one owner, and the Runtime keeps
no parallel order or position state machine beside Nautilus:

| Fact | The one owner | How it is kept |
| --- | --- | --- |
| Positions, open orders, fills | The venue; the in-process projection is the Nautilus Cache | Startup reconciliation (`reconciliation=True`, a lookback covering the oldest open Plan's creation time) rebuilds the Cache before the Strategy starts; the 5 s open-order and position checks keep it converged. PostgreSQL's open Plans supply a bounded symbol query scope when the Cache and venue are flat. Reconciliation applies only the venue's own orders and fills: it never generates one to match a position report (`generate_missing_orders=False`), and the Binance client's fill reports name each venue trade once. No Cache database: a restart is the same reconciliation a start is. |
| Whether the Cache agrees with the venue | The venue's own positions, read by the Runtime | Signed `positionRisk` every 30 s. A failed read is unknown, never flat; a disagreement on two reads in a row is unexpected exposure. Detect-only. |
| Trading intent (a plan) | `trading_trade_plans` | Written when the entry is admitted (before its order exists), when its position opens, and when it ends. Read back only as intent — instrument, direction, distances, maximum holding time — never as order or position state. |
| Execution event log | `trading_execution_observations` | An append-only journal of verdicts, orders, fills (with the venue's commission) and positions, one row per transaction. |
| Realized PnL | The fill journal | Exit minus entry notional, signed by direction, less every commission; folded by the read models. Not a Nautilus position field. |
| Signals, operator Commands, control switches | PostgreSQL | Unchanged. |
| Tradable universe | The Runtime's route catalogue, from Nautilus' Binance instrument provider | USDT-settled `PERPETUAL` contracts in `TRADING` status; `TRADIFI_PERPETUAL` is never routed. |

The Strategy converges the Cache on intent every five seconds, and on every fill and
position event: a position whose entry order is terminal gets one reduce-only stop and
one reduce-only take-profit on the mark price, a missing one is placed again, a position
past its maximum holding time is closed, and when one of the Runtime's own closing legs
closes a position the orders left on its instrument are canceled. Exposure no plan
claims blocks new entries and is recorded; nothing is ever flattened because the picture
is unclear. App supplies process, database and probe composition and never reads a
private member of a Nautilus object. Paper and live use the same path with their
respective account and environment; neither mode turns a local command into a fill.

The Cache is not trusted alone (#680 PR-3). Nautilus 1.231.0 reconciliation could
"repair" a disagreement by inventing a fill, and on 2026-09-23 it twice closed an open
Demo position in the Cache while the venue still held it, after which the Strategy
canceled the position's protection. So a close none of the Runtime's closing legs sent
is only a Cache event: the stop, the take-profit and the plan stay until a venue read
confirms the instrument flat, and until then the instrument is unexpected exposure.
Reduce-only protection on an instrument with no Cache position is canceled only on a
flat venue read. Entries need a venue read younger than two minutes that agrees with the
Cache (`venue_unverified` otherwise). `/flatten account` closes, with reduce-only market
orders, what the Cache holds and what only the venue holds. Inspect the Runtime's
risk observations and Nautilus' rotated WARN/ERROR logs for exposure incidents.

When Binance triggers a protective Algo order, its regular child order gets a new
venue ID. The execution client checks the signed parent Algo receipt for that exact
child ID, then updates Nautilus' cached order ID before replaying the child's real
trades. An unmatched or incomplete receipt leaves the discrepancy unresolved (#699).
On restart, the signed receipt also identifies a historical protective child in
Nautilus' order report before replay. A closed Cache Position settles its open
Plan with that leg's reason only when the Position's opening order matches the
Plan entry and its closing order has a real fill.

The pure Trading engine imports no adapter, database or Nautilus engine and has
no order authority. The historical OI v5 Signal lane is not scheduled by Workers.

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

The four independent tasks refresh the followed list, collect receipts, detect concentrated
net-buy episodes, and sample prices. The refresh task owns every provider call to the roster site
and publishes every unique valid address in a complete source response; the collector reads the last
published version out of PostgreSQL and makes no roster call at all, so a slow or throttled provider
cannot stop collection (#649 §5.1). Detection and first notification have no balance, bags, external
quote, or model dependency. Complete transaction facts and derivation progress commit
atomically. Membership versions change only with the address set; source statistics do not gate subscription.
A single continuous receipt prefix owns both collection and detection cutoffs. Real missing receipts
stop the turn and retry durably; optional metadata cannot block it. The detector calculates the one window from the same fill set,
with explicit member coverage, pricing, and exclusion reasons.

An episode retains an immutable first snapshot and an independently updated current
snapshot. Its logical first notification uses the existing market intent/delivery
owner. Before the first attempt, eligibility and freshness are checked against actual
persisted state, and the evidence is re-evaluated by the detector's own pure function at the
collector's committed cutoff `(scanned_block, scanned_log)`: facts inside that cutoff decide the
report, facts above it never hold it back, and a card whose evidence is not yet derived is deferred
with its own due time rather than skipped. The attempted payload then freezes. Price samples remain independent
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
