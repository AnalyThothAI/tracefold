# Architecture

Tracefold is one Python codebase/image with two mutually exclusive runtime
composition roots, one CLI, one React console, and one PostgreSQL database.
It has exactly two business capabilities, News V3 and Macro. The architecture
remains Kappa/CQRS: append-oriented material facts are the only business
truth; deterministic current views and bounded immutable model publications
are derived state.

## Data flow

```text
OpenNews Strategy WSS / official macro sources
  -> tracefold workers (RabbitMQ is the News transport plane)
  -> PostgreSQL material facts + bounded projection state + native model state
  -> single-writer read models or immutable publications
  -> tracefold serve
  -> HTTP / React
```

`tracefold serve` initializes only public HTTP/static, read repositories, and
serve telemetry. `tracefold workers` initializes Macro acquisition, the bounded
external/model capabilities, one short-projection CPU lane, singleton runtime
status, the RabbitMQ-driven News consumers when News is enabled, and one EDF
projection coordinator for the Macro frontier. Macro workers recover
exclusively by re-reading PostgreSQL facts, the typed Macro frontier, native
Fed-document model state, and queues on bounded code-owned clocks; News
consumers recover by re-consuming durable broker queues plus database
idempotency keys. There is no database wake plane or in-memory correctness
dependency. Provider raw frames remain inputs until normalized and persisted
as material facts.

The deployment composition has four required boundaries: PostgreSQL, one
successful migration job, Serve, and Workers. `make up` is only their
fail-closed lifecycle orchestrator; it does not merge the two runtime roots.
On an empty PostgreSQL volume, the image's `initdb` hook creates the
non-login owner plus least-privilege Serve, Workers, and migrate roles from
separate password files, then revokes the bootstrap login before the migration
job runs. That hook is never replayed against a non-empty cluster. Repeated
startup therefore preserves the database and operator-owned credentials, while
an unknown existing schema or missing role fails instead of being implicitly
hard-cut.

The same project-scoped application image contains the Python service and a
production React build. Migration, Serve, and Workers use that exact image and
build revision with different commands and credentials.
`make up` builds the image once and recreates only migration, Serve, and
Workers; it starts PostgreSQL when absent but does not recreate a running
PostgreSQL container. Serve owns the static console and public HTTP
boundary; Workers exposes only its loopback operational boundary. Image
construction and Compose startup do not become alternate configuration
sources: `tracefold init` remains the single generated-default authority and
`~/.tracefold/config.yaml` remains the single live application config.

The projection coordinator executes exactly one semantic shard at a time.
A productive turn yields cooperatively and immediately rereads every typed
frontier head so a real backlog can converge. Failed and idle turns wait on the
fixed 250 ms code-owned cadence to prevent contention from becoming a hot loop;
that cadence is scheduling policy, not a correctness authority.

## Truth, control state, and derived state

Material facts include:

- news: canonical provider Item facts admitted by the operator's OpenNews
  Strategy allowlist in `news_items` (provenance union, provider metadata,
  raw first line, first ingest mode);
- macro: revision-preserving `macro_series_facts`, `macro_release_facts`,
  `macro_documents`, and `macro_fed_official_role_facts`, plus Macro's general
  market observation facts `market_instruments`, `market_observations`,
  `market_settlements`, and `market_position_facts` (the exchange, ETF,
  futures-proxy, settlement, and CFTC positioning inputs of the six modules).

Current read models are `news_events` (plus `news_event_members`,
`news_event_bands`, `news_event_assets`) and the six stable rows in
`macro_module_current`. Each uses stable product/window/target identity, has
exactly one runtime writer, is rebuildable from facts, and writes zero serving
rows when its business payload is unchanged. `news_events` is rebuildable by
replaying `news_items` through the Deduper (`tracefold news replay` performs
the same computation in memory). OpenNews's raw `coins` annotation remains
source evidence in `news_items.provider_metadata`; the Gate derives the bounded
`grounded_assets` from it and the read API exposes both.

OpenNews connection state in `news_ingest_state`, explicit incident intervals
in `news_opennews_incidents`, News control state (`news_control_state`),
queues, leases, retries, native model runs/jobs, and terminal events are
control state. The typed Macro frontier stores stable
module identity, input fingerprint, earliest deadline, lease, failure,
and publication checkpoints. `first_dirty_at_ms` records the causal change,
`deadline_at_ms` is the freshness SLA, and `next_attempt_at_ms` is only an
eligibility clock for a scheduled recheck or retry. An eligible shard may run
before its deadline; the deadline is never a start gate. Retry attempts and
terminal reasons are likewise queue policy, not facts.
`news_verdicts` (Triage decisions bound to a policy version) and rows in
`macro_document_analyses` are derived model outputs bound to frozen evidence;
they are not material facts. `news_deliveries` is the one-attempt outbound
ledger keyed by `(event_id, kind)`; there is no retry, lease, or backfill.
`news_event_labels` is the learning-plane truth for evaluating decisions.

The current schema is exactly 28 tables: the eleven `news_*` tables, the ten
`macro_*` tables plus the four general market fact tables owned by Macro, and
the three platform tables `alembic_version`, `queue_terminal_events`, and
`workers_runtime`.

## Package map

```text
tracefold.news
  opennews.py         canonical OpenNews frame adapter (raw_text, provenance)
  bus.py              broker envelope, routing keys, error classes, Publisher/Consumer protocols
  titles.py           content-block title extraction + pinned prefix/suffix tables
  exact_atom_identity.py comparison normalization, event family, windows
  tokens.py / minhash.py  comparison tokens, MinHash 32x4 band keys
  gate.py / storyline.py  deterministic admission, priority, grounded assets, storyline keys
  events.py           the Deduper transaction (admit_item)
  triage_rules.py     decide() post-rules (DecidePolicy), throttle, fail-closed fallback
  agents/             the Triage structured call and its byte-frozen prompt
  delivery.py / control.py  cards, control commands
  consumers.py        Receiver, Recovery, Deduper, Triage, Deliverer, Janitor
  repository.py / query_specs.py  news_* access and audited reads
  eval/               offline label evaluation, decision replay, hits replay

tracefold.macro
  registry.py    code-owned Dataset Registry and six-module membership
  acquisition.py clock-driven claim, provider-I/O, fact and cursor flow
  calculations.py versioned calculation registry and transparent features
  projection.py  six current decision modules
  market_facts.py / market_facts_repository.py  general market observation, settlement,
                 and positioning facts (market_instruments, market_observations,
                 market_settlements, market_position_facts)
  fed_analysis.py evidence-bound FOMC/speech analysis contract
  fed_document_agent.py one structured model call over the official body's evidence catalog

tracefold.integrations
  provider and external-system adapters: OpenNews, RabbitMQ, Feishu, macro sources

tracefold.platform
  config, PostgreSQL/Alembic (baseline + two chained hard cuts), telemetry, paths,
  bounded resource primitives, docker host translation

tracefold.app
  composition, repositories, the worker root package (`app/workers/`), HTTP, and CLI
```

The two business package roots are their public Python interfaces:
`tracefold.news` and `tracefold.macro`. Consumers outside an owning package
import from the root only. Internal subpackages may change without creating a
repository-wide import graph.

The application composition root and concrete provider adapters are private
implementation collaborators, not product consumers. Where one of them must
construct a repository, schedule an internal worker, or reuse the exact pinned
parser/composer implementation behind a public protocol, its package-private
import is enumerated exactly by the architecture harness. Those named seams are
not re-exported, compatibility interfaces, or available to feature callers;
all public models and protocols still come from the package root.

The dependency direction is:

```text
app -> integrations + business packages + platform
integrations -> business package interfaces + platform
news -> platform
macro -> platform
platform -> Python / third-party libraries only
```

Business packages never import `tracefold.app`, provider integrations, or each
other. Transport adapters do not own business rules. The Workers root and its
private TaskGroup loops live in `tracefold.app.workers`; platform exposes only
bounded resource/projection/model candidate contracts. Queue state machines and
read-model behavior stay with their business owner. These rules are executable
in `tests/architecture/test_backend_boundaries.py`.

A Provider is an integration adapter, not a product layer, registry, or second
source of truth. Each adapter translates one upstream transport and error model
into a business-package protocol. The adapters are OpenNews (the authenticated
Strategy WSS plus the official Strategy list/hits endpoints), RabbitMQ
(`aio-pika`), Feishu (the custom-bot webhook), and the Macro source client
(Treasury, FRED, BLS, BEA, Federal Reserve pages, CFTC, Cboe, Nasdaq, Yahoo
Chart, and Binance public spot klines as one Macro dataset). No provider owns
a durable queue. Expected provider failures stay inside the owning bounded
loop; an unhandled child exception is deliberately a Workers-root failure and
the container restarts the single process.

SQL ownership follows the same boundary: News owns `news_*`; Macro owns
`macro_*` plus `market_instruments`, `market_observations`,
`market_settlements`, and `market_position_facts`. Platform owns Alembic,
`queue_terminal_events`, and `workers_runtime`. News makes no cross-domain
read: its single read-only seam (`macro_module_current` as Analyst evidence)
went with the Analyst lane in #57. Macro has no live or hidden dependency on
News. The architecture gate checks SQL table references against the generated
current schema.

## Transaction ownership

Application services and workers own transaction scope. Repository writes use
the supplied connection and never expose commit switches or open hidden
transactions.

Important atomic units are:

- one Macro acquisition completion: normalized fact insert, cursor advance, and
  compare-and-set target completion;
- current read-model write plus acknowledgement of the exact claim;
- one accepted OpenNews frame: NewsItem upsert with provenance union plus its
  Event assignment (new Event, bands, assets, or membership);
- one Triage verdict insert; one delivery begin or settle;
- immutable Fed document analysis plus completion of its exact native job;
- retry or terminal transition plus mutation of its source queue row.

Provider, model, subprocess, filesystem, and network I/O occurs outside
database transactions.

Each Worker database session owns exactly one bounded PostgreSQL transaction.
It installs its statement and transaction limits as transaction-local settings
in one setup round trip, so PostgreSQL is the native deadline authority for all
SQL in that session. Transaction exit restores the connection automatically;
there is no session reset round trip. Awaiting DB, CPU, finite-operation, and
model work adds only a bounded completion grace so the native result wins at
its deadline. If an asyncio wrapper callback is delayed, an already-completed
native future is consumed directly. A typed recurring business-DB future that
remains alive beyond the grace is a local loop failure: its permit stays bound
to native completion and the loop retries on its natural cadence. Control-DB,
model, CPU, cleanup, and otherwise unclassified overruns remain fatal. This
decision uses the exception's typed physical capability, never an error-string
or operation-name prefix. A business-lane PostgreSQL idle-transaction
disconnect is bounded admission failure. The idempotent runtime-heartbeat
child retries only precise transient database failures; 15 seconds without a
fresh heartbeat degrades readiness without killing the root, while recovery
restores readiness. Pinned-singleton loss, invariant failures, and an unfinished
native control future remain fatal. Only an explicitly classified true
external provider seam may translate a finite-operation overrun into its
existing durable retry, degradation, or terminal policy; doing so never
releases the shared capability permit before the underlying future actually
finishes. Frontier-backed projection turns therefore have phase-native
deadlines and no aggregate fatal watchdog.

The Macro projection claim lease covers the complete legal phase envelope (30
seconds). News consumers have no frontier lease; the broker's
single-active-consumer and per-message ack are their fences.

## Workers task set

The Workers root TaskGroup contains exactly: `workers-probe` (loopback
health/readiness/metrics), the News consumer tasks when News is enabled
(`news-receiver`, `news-recovery`, `news-deduper`, `news-triage`,
`news-deliverer`, `news-janitor`), one `durable-due-N` task per Macro
acquisition clock family, `projection-edf` (the Macro frontier
coordinator), `model-arbiter` (Fed document analysis), and `workers-control`
(singleton lock, heartbeat, runtime row). There is no periodic market poll,
stream ingester, identity backfill, or universe sync task.

## Product flows

### News

News V3 is a broker-driven Event pipeline. RabbitMQ is the only transport,
buffer, retry, concurrency, and dead-letter plane; PostgreSQL holds facts,
decisions, and audit; every write is idempotent by key. The Story/Brief/RSS/
pinned-WorldMonitor lane and the title-translation lane are retired.

```text
OpenNews account Strategies (news.opennews_strategy_ids; validated at startup)
  -> authenticated persistent WSS; server pushes strategy.triggered; no app subscribe frame
  -> Receiver publishes each accepted frame to x:news with publisher confirms
     (routing key raw.opennews.<strategy_id>; recovery frames use raw.recovery.<strategy_id>)
  -> q:news.raw [single-active-consumer] Deduper:
       Item upsert (provenance union) -> content-block title + pinned normalization
       -> exact fingerprint / MinHash 32x4 LSH near-duplicate + strong-fact veto
       -> Event new|member (family window) -> Gate (provider-graded grounded_assets,
          macro/energy lexicon, PR-template veto, low-signal switch) -> preliminary
          storyline key; a stronger later member re-gates a suppressed Event
       -> publish event.<family>.<priority> only for admission=candidate
  -> q:news.triage [prefetch = news.triage.concurrency, handled concurrently] Triage:
       one structured call (frozen system prompt, <event> -> <gate> -> <event_status> status bar,
       one bounded retry for a fast retryable model failure) -> final storyline key from the
       verdict (written back) -> decide() policy -> verdict row (title_zh, audience, prompt sha,
       input sha, preliminary + final status snapshots, named rule) -> publish verdict.push
       (an escalate rides the same routing key at AMQP priority 5; there is no second model call)
  -> q:news.deliver [single-active-consumer] Deliverer: begin(sending) -> one Feishu attempt
       -> settle sent|terminal; paused -> terminal/delivery_paused; crash between send and ack
       -> ambiguous_after_crash
  -> news.retry (one 30 s TTL lane -> back to x:news): TransientError counted (3 attempts),
     DeferError uncounted; x:news.dlx -> q:news.dead for permanent/exhausted/crashed messages
  -> Janitor: outbox catch-up (unpublished candidates), band expiry, 30-day retention,
     broker depth snapshot
  -> Serve: /api/news/feed, /api/news/events/{event_id}, /api/news/status
```

Ownership: `tracefold.integrations.rabbitmq` is the only module that imports
`aio_pika`; `tracefold.news.bus` owns the envelope, routing keys, error classes,
and Publisher/Consumer protocols. `tracefold.news.consumers` holds the six
consumers wired by `tracefold.app.workers._wire_news_pipeline`; they run as
asyncio tasks in the single Workers process but coordinate only through the
broker and PostgreSQL keys, so they can be scaled out without code changes.
News consumers use their own four-slot database lane
(`WorkerDatabase.run_news`) so Macro backlog never starves a live Event;
a lane admission timeout is a `DeferError` (uncounted requeue), a statement
overrun is a `TransientError` (counted).

Identity: `news_items.item_id = sha256(source_id, params.id)`;
`news_events.event_id` is the leader item id. `tracefold.news.titles`
extracts the first content block (skipping URL-only, label-only, `reply/quote:`
lines and pinned wire source labels/suffixes; exchange names and `@handles`
are subjects and stay — `@Krakenfx launches ...` keeps `Krakenfx`),
`tracefold.news.exact_atom_identity`
normalizes for comparison, `tracefold.news.tokens` + `minhash` produce the
band keys stored in `news_event_bands`, and `tracefold.news.events.admit_item`
is the single Deduper transaction. Fingerprints of at most two tokens never
share an Event.

Gate and storyline (`tracefold.news.gate`, `tracefold.news.storyline`) are pure
functions and keep no name table of their own: grounded assets are the
provider's grade B+/A/A+ coin tags plus any literal `$TICKER` cashtag (the
provider already resolved Bitcoin -> BTC, Home Depot -> HD); `CL`/`XYZ-CL` is
grounded only in energy context and a short stop-list drops English-word tags.
The Gate does not decide relevance: every Item is a `candidate` unless it is a
recovery replay, a deterministic `listing` frame, a law-firm template notice
(strong template phrases always; weak ones only without a grounded asset), an
under-80 market-telemetry frame, or — behind `news.gate.suppress_low_signal`,
default off — an ungrounded, non-macro social post under 70. A member that
joins a suppressed Event with stronger evidence (score >= 80, an A/A+ grounded
tag, or a different source) re-gates it in place and it publishes once.
Priority is `high` (AMQP priority 5) for score >= 90, watchlist hits, listing
frames, or rate/yield macro. The preliminary storyline key (status bar only)
is theme-first (`crypto_treasury`, `mideast_energy`, `rates`, `trade`,
`china_macro`, `metals`, `us_equity_macro`, `us_macro_data`), then the first
A/A+ or cashtag asset; the final key is computed after Triage from the
verdict's grounded primaries and scope, written back to `news_events`, and
used by every window query, throttle, and mute.

Triage (`tracefold.news.agents.triage_model`, `tracefold.news.triage_rules`)
never retrieves: the Deduper computes `event_status` (storyline window facts)
and the consumer passes it last in the human message. The byte-frozen system
prompt is English (instructions) and every text field the verdict returns is
Chinese: `headline_zh` (the card header), `why_zh` (the one card sentence), a
console-only `title_zh` (the faithful Chinese title), and an `audience`
(crypto / us_equity / macro / none). `decide()` owns the final decision under
a `DecidePolicy` whose defaults are the live policy and whose values come from
`news.policy`: mute -> drop; noise -> drop; magnitude >= 3 with a direction or
macro scope -> escalate; high priority + push -> escalate; model push/escalate
intent, actionable, magnitude >= `min_push_magnitude` (1) and a direction ->
push (`model_push_actionable`); unclear direction with a clear event type
(product, listing, delisting, regulation, hack, exploit, partnership, filing)
at magnitude >= 2 -> push (`unclear_but_clear_event`); other unclear -> drop;
watchlist primary at magnitude >= 1 -> push; else drop (`below_threshold`).
Storyline throttling (switch `storyline_throttle`) keeps the window-max +
direction-flip rule for `asset:` keys and caps `theme:`/`macro:` keys at
`theme_cap_4h` (3) pushes per 4 h unless magnitude exceeds the window max or
the direction flips; the hourly cap (switch `hourly_cap_enabled`,
`news.push.hourly_cap`, default 30) throttles pushes only. Every path names
its rule; nothing drops silently. A fast retryable model failure (timeout,
rate limit, connection) earns one more attempt inside the deadline; model
failure is degraded, not silent: `rule_baseline` (watchlist primary, or score
>= 80 with a grounded asset) still pushes, everything else drops with
`degraded=true`, and three consecutive failures open a 60-second circuit that
also opens a `triage_circuit_open` incident. `news_verdicts` stores
`model_decision`, `rule_baseline_decision`, `final_decision`, `override_rule`,
`throttled_by`, `degraded`, and a replayable trace (latency, tokens, model
attempts, prompt sha, input sha, the preliminary and final status-bar
snapshots, the final storyline key).

There is no second model stage: one Event gets one structured judgment and one
card (issue #57). `escalate` stays a `decide()` outcome — a high-importance
push that rides the same `verdict.push` routing key at AMQP priority 5 and
wears a ⚡ card header — and never triggers another model call. The retired
Analyst lane (`q:news.deep`, the `verdict.escalate`/`verdict.deep` routing
keys, the evidence bundle and its `verify_verdict()` gate, follow-up cards)
left `stage='deep'` verdicts and `kind='followup'` deliveries as historical
rows that are never written again, and topology declaration deletes an old
`news.deep` queue at startup.

Delivery (`tracefold.news.delivery`, `consumers.DelivererConsumer`) renders the
reader contract (`news_delivery_card_v8`): the header is `headline_zh` (⚡ when
the decision is escalate; it falls back to `title_zh`, then the original
title), the first body line is `why_zh`, and the second is the facts in plain
words — direction label, magnitude label, the tickers the model called primary
and the Gate grounded, source（N 条报道）, and the leader item's publication
time in the reader's zone (UTC+8) — followed by a 打开来源 button and a small
`Tracefold · <event_id[:8]>` note. There is no original headline line, no
translated title, no event type or scope enum, no provider score, and no line
labelled as AI: those internals stay in the console and `tracefold news why`.
AI copy is sanitized (URLs fall back to the code-owned title). There is no
retry: `news_deliveries(event_id, kind)` (`kind` is always `first`) is
inserted as `sending` before the single HTTP call and settled `sent`/`terminal`;
interrupted rows are terminalized at startup. Recovery items, suppressed
events, and muted storylines never deliver; a paused lane settles
`terminal/delivery_paused` instead of holding an unacked message; the hourly cap
lets only escalates through. Control state (`news_control_state`) is written by
`tracefold news control` and read by Triage and the Deliverer on every message.

Incidents and recovery: WSS transport/auth/protocol/idle failures, broker
backpressure/unavailability, and Triage circuit opens are rows in
`news_opennews_incidents`; reconnect closes transport incidents and requests
recovery, which pages the official Strategy hits endpoints for the closed
interval and publishes `raw.recovery.*` frames (`admission=recovery`, never
delivered). Dead letters are operator-visible through `tracefold news dlq
inspect|replay|purge`.

Storage is exactly eleven tables: `news_ingest_state`,
`news_opennews_incidents`, `news_items`, `news_events`, `news_event_members`,
`news_event_bands`, `news_event_assets`, `news_verdicts`, `news_deliveries`,
`news_control_state`, `news_event_labels`. Read queries are registered in
`tracefold.news.query_specs` for the query audit.

Learning plane: `news_event_labels` hold operator labels written by `tracefold
news label` (`good`, `noise`, `late`, `wrong_direction`, `dup`, `missed`) on
any Event, pushed or held; `tracefold news eval` walks every Event of the
window (Gate-suppressed ones count as `suppressed`), treats
`good`/`wrong_direction`/`late`/`missed` as "moved" and `noise`/`dup` as
"flat", and reports precision@push, the guardrail `missed_rate` and
`false_push_rate`, suppressed/missed/throttled-mover rates, and per-admission /
per-rule / per-throttle / per-asset-class / per-audience / per-event-type
confusion tables; `tracefold news replay-decisions` re-runs `decide()` over
stored verdicts with a candidate `DecidePolicy` (defaults from `news.policy`,
switches for storyline throttling and the unclear-event rule) against the
final storyline status snapshot; `tracefold news replay <hits.json>
[--gate-policy]` replays provider hits through Deduper+Gate without a model or
broker and lists every Event with its admission, grounded assets, and
preliminary storyline; `tracefold news why <event_id>` prints one Event's whole
chain (item, gate, triage, decide, delivery) with a one-line outcome.
`tests/fixtures/news_v3_hits_sample.json` and
`news_v3_hits_recall_sample.json` are the golden replay corpora and
`news_v3_expectations.json` the trajectory-prefix regression over them. There
is no market-mark or price-reaction lane.

### Macro

```text
code-owned Dataset Registry + Coverage Manifest
  -> one of six clock families
  -> macro_acquisition_targets claim
  -> free official / exchange / disclosed proxy adapter
  -> typed append-only macro or general market fact + target cursor/current state
  -> static dataset -> calculation -> module dependency graph
  -> typed affected-module frontier
  -> one EDF module-local reducer
  -> one stable macro_module_current closure
  -> persisted-only overview and module reads

official FOMC / speech body + effective-dated role fact
  -> macro_document_analysis_jobs claim
  -> immutable evidence-bound document analysis
  -> institutional stance + officials communication distribution
```

The acquisition clock families are `intraday_market`, `daily_settlement`,
`scheduled_release`, `official_state`, `official_document`, and explicit
`backfill`. The five steady families are explicit private due loops over one
target table, not Worker objects or a uniform bundle poller. Claims use
`SKIP LOCKED`; provider I/O occurs outside database transactions; completion
atomically writes facts, cursor, and current target success/error state.
Unchanged source content writes zero fact rows. Revisions append a new fact and
never overwrite history.

Macro bounded history reads and the Calculation Registry's typed builder
contracts execute inside the six module payload builds, outside the write
transaction. There is no second generic feature-materialization engine. A
short compare-and-set write phase
publishes the stable module row and projection fingerprint. Unchanged module
payloads write zero serving rows.

The Dataset Registry fixes ownership, concept identity, source role, clock,
adapter, trust tier, freshness, criticality, and module membership in code.
Every concept has one primary current source and may have an explicitly labelled
official-history or proxy source. Dataset and source identities stay explicit
and are never blended. The Coverage Manifest contains only
capabilities that the supported free-data system can truthfully provide;
missing paid or unimplementable capabilities are deleted rather than displayed
as permanent product gaps. Operator config only enables source families;
cadence, lease, timeout, batch, and resource limits are code-owned.

OpenBB is not a runtime dependency or provider router. It does not supply data
or entitlement, and routing an existing source through it would add a second
provider/configuration/error layer without changing source identity. Macro uses
its direct, narrow adapters and never adds a provider waterfall around them.

The current nominal and real curves come from Treasury, with FRED as labelled
history. CPI and labor release facts come from BLS, while GDP, PCE, and core PCE
release facts come from BEA's public official release pages; the matching FRED
series are history only. Release timestamps are parsed from the official
release clock, never substituted with ingestion time.

Coverage (`complete`, `partial`), Current Health (`current`, `degraded`,
`unavailable`), and History Depth (`complete`, `partial`, `insufficient`,
`not_required`) are independent descriptive axes. Optional history cannot
degrade current-state health or reader-facing History Depth. Dataset rows also
expose market and source state;
closed and maintenance sessions do not age the last expected market bar against
wall time.

The six product modules are `rates_fed`, `economy_inflation`,
`liquidity_funding`, `credit`, `volatility`, and `cross_asset`. One code-owned
module descriptor fixes each ID, label, schema version, and builder key. Each
has one explicit typed payload (rates v8, economy v6, liquidity v5,
credit/volatility v7, and cross-asset v8), deterministic module-specific analysis, exact
market timestamps, natural publication cadence, source roles, importance
ranks with factor explanations, and evidence lineage. Release payloads keep expected, actual, surprise,
revision, publication time, and Registry-owned seasonal-adjustment semantics
distinct. ETF daily history is the Nasdaq public
five-year lane; Yahoo supplies ETF intraday prices and paired intraday/daily
continuous-contract futures proxies. No generic chart-array contract survives. The Calculation
Registry records every feature's inputs, formula version, windows, minimum
observations, units, gap policy, freshness, baseline, and output shape.
The Natural Change Calculation Registry separately fixes every Dataset's
cadence-native windows, minimum observations, formula, unit, revision/surprise
rules, bounded-gap policy, and output schema. Exact month/quarter lags cannot
fall back to an older available row while retaining the requested window label.
Treasury shape, matched breakevens, normalized asset returns, 30/90/252
common-daily-return correlations, credit ladder history, and funding
comparisons remain deterministic. Correlation pair facts are undirected and
exclude diagonals; the payload's presentation contract owns the default window
and mirrored unit-diagonal display rule. Credit exposes spread,
funding cost, bank supply, and borrower quality concurrently and never reduces
them to a score.

Rates v8 also carries the current official FOMC calendar snapshot and recent
Treasury auction-demand facts. The calendar adapter emits one immutable
revision across all meetings on the official page, so an official reschedule
replaces the prior snapshot in the read model without deleting its audit facts.
Auction bid-to-cover, Bill discount rate, investment rate, high yield, offering
amount, and bidder award shares enter the existing release-fact family from
Treasury Fiscal Data as distinct fields. The competitive
close is a scheduled clock; no result publication time is invented. SOFR has
one Registry owner and one fact identity while the dependency graph makes that
same fact available to both Rates and Liquidity.

Macro has no second judgment publication, daily narrative, or archive product.
The overview is a compact index over the six current module rows; each module
is a deterministic descriptive view over persisted facts.
One unavailable module affects only that module, and read requests never invoke
a provider, model, backfill, or projection.

Fed document analysis is the only model-derived Macro state. It receives one
bounded official body plus effective-dated role/prior-signal context, verifies
exact excerpts against that body, and inserts one immutable analysis for the
exact document/model/prompt identity. It feeds the descriptive `rates_fed`
module as a supporting capability and never gates official Rates/Fed current
health. Publication, completion of the native job, advancement of the derived
Dataset projection state, and dirtying the `rates_fed` frontier share one
transaction. A maintenance rebuild derives the same state from immutable
analyses and native jobs.

Migration history is squashed. `20260818_0275_baseline` is the single root
revision: it executes the frozen `current_schema_20260818_0275.sql` dump
(every table, index, constraint, seed row of the schema as it stood after the
News V3 hard cut and Radar removal) plus `runtime_roles.sql`, and it is
irreversible. Two chained revisions follow. `20260818_0276_review_49_hard_cut`
drops the retired title-translation, DEX discovery, token profile, token
image, and Radar-era checkpoint tables. `20260818_0277_gmgn_lane_removal`
drops the whole GMGN lane: the social evidence tables (`raw_frames`, `events`,
`event_entities`, `enriched_events`, `collector_pending_items`,
`event_anchor_backfill_jobs`), token identity and registry tables
(`token_evidence`, `token_intents`, `token_intent_lookup_keys`,
`token_intent_evidence`, `token_intent_resolutions`, `registry_assets`,
`asset_identity_evidence`, `asset_identity_current`, `us_equity_symbols`),
DEX/CEX market data tables (`market_ticks` with its default partition,
`market_tick_current`, `price_feeds`, `cex_tokens`), the persisted live
broadcast journal (`persisted_live_events`), `provider_circuit_state`, and the
News market-mark table (`news_event_market_marks`), plus the
`forbid_market_fact_update()` trigger function and the terminal-evidence rows
of the dropped queues. Neither chained revision has a downgrade. Earlier hard
cuts live only in git history; a fresh database and a database upgraded
through the chain reach byte-identical schemas.

See [Public Contracts](CONTRACTS.md), [Operations](OPERATIONS.md), and
[Frontend Architecture](FRONTEND.md) for the other current authority surfaces.
