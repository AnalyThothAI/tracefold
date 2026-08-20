# Architecture

Tracefold is one Python codebase/image with two mutually exclusive runtime
composition roots, one CLI, one React console, and one PostgreSQL database.
It has exactly one business capability, News V3. The architecture
remains Kappa/CQRS: append-oriented material facts are the only business
truth; deterministic current views and bounded immutable model publications
are derived state.

## Data flow

```text
OpenNews Strategy WSS
  -> tracefold workers (RabbitMQ is the News transport plane)
  -> PostgreSQL material facts
  -> single-writer read models
  -> tracefold serve
  -> HTTP / React
```

`tracefold serve` initializes only public HTTP/static, read repositories, and
serve telemetry. `tracefold workers` initializes the bounded external
capability, singleton runtime status, and the RabbitMQ-driven News consumers
when News is enabled. News consumers recover by re-consuming durable broker
queues plus database idempotency keys. There is no database wake plane, no
projection/EDF coordinator, no CPU-process lane, and no in-memory correctness
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

## Truth, control state, and derived state

Material facts include:

- news: canonical provider Item facts admitted by the operator's OpenNews
  Strategy allowlist in `news_items` (provenance union, provider metadata,
  raw first line, first ingest mode).

The current read model is `news_events` (plus `news_event_members`,
`news_event_bands`, `news_event_assets`). It uses stable product identity, has
exactly one runtime writer, is rebuildable from facts, and writes zero serving
rows when its business payload is unchanged. `news_events` is rebuildable by
replaying `news_items` through the Deduper (`tracefold news replay` performs
the same computation in memory). OpenNews's raw `coins` annotation remains
source evidence in `news_items.provider_metadata`; the Gate derives the bounded
`grounded_assets` from it and the read API exposes both.

OpenNews connection state in `news_ingest_state`, explicit incident intervals
in `news_opennews_incidents`, News control state (`news_control_state`),
and broker queues are control state. Retry attempts and terminal reasons are
likewise queue policy, not facts. `news_verdicts` (Triage decisions bound to a
policy version) are derived model outputs bound to frozen evidence; they are
not material facts. `news_deliveries` is the one-attempt outbound
ledger keyed by `(event_id, kind)`; there is no retry, lease, or backfill.
`news_event_labels` is the learning-plane truth for evaluating decisions.

The current schema is exactly 13 tables: the eleven `news_*` tables and the two
platform tables `alembic_version` and `workers_runtime`.

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

tracefold.integrations
  provider and external-system adapters: OpenNews, RabbitMQ, Feishu

tracefold.platform
  config, PostgreSQL/Alembic (baseline + three chained hard cuts), telemetry, paths,
  bounded resource primitives, docker host translation

tracefold.app
  composition, repositories, the worker root package (`app/workers/`), HTTP, and CLI
```

The business package root is its public Python interface: `tracefold.news`.
Consumers outside the owning package import from the root only. Internal
subpackages may change without creating a repository-wide import graph.

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
platform -> Python / third-party libraries only
```

Business packages never import `tracefold.app`, provider integrations, or each
other. Transport adapters do not own business rules. The Workers root and its
private TaskGroup loops live in `tracefold.app.workers`; platform exposes only
bounded resource contracts. Queue state machines and
read-model behavior stay with their business owner. These rules are executable
in `tests/architecture/test_backend_boundaries.py`.

A Provider is an integration adapter, not a product layer, registry, or second
source of truth. Each adapter translates one upstream transport and error model
into a business-package protocol. The adapters are OpenNews (the authenticated
Strategy WSS plus the official Strategy list/hits endpoints), RabbitMQ
(`aio-pika`), and Feishu (the custom-bot webhook). No provider owns a durable
queue. Expected provider failures stay inside the owning bounded
loop; an unhandled child exception is deliberately a Workers-root failure and
the container restarts the single process.

SQL ownership follows the same boundary: News owns `news_*`; platform owns
Alembic and `workers_runtime`. News makes no cross-domain read: its single
read-only seam (`macro_module_current` as Analyst evidence) went with the
Analyst lane in #57, and the Macro tables themselves went in #68. The
architecture gate checks SQL table references against the generated current
schema.

## Transaction ownership

Application services and workers own transaction scope. Repository writes use
the supplied connection and never expose commit switches or open hidden
transactions.

Important atomic units are:

- one accepted OpenNews frame: NewsItem upsert with provenance union plus its
  Event assignment (new Event, bands, assets, or membership);
- one Triage verdict insert; one delivery begin or settle.

Provider, model, filesystem, and network I/O occurs outside database
transactions.

Each Worker database session owns exactly one bounded PostgreSQL transaction.
It installs its statement and transaction limits as transaction-local settings
in one setup round trip, so PostgreSQL is the native deadline authority for all
SQL in that session. Transaction exit restores the connection automatically;
there is no session reset round trip. Awaiting DB, finite-operation, and model
work adds only a bounded completion grace so the native result wins at
its deadline. If an asyncio wrapper callback is delayed, an already-completed
native future is consumed directly. A typed recurring business-DB future that
remains alive beyond the grace is a local loop failure: its permit stays bound
to native completion and the loop retries on its natural cadence. Control-DB,
model, cleanup, and otherwise unclassified overruns remain fatal. This
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
finishes.

News consumers have no frontier lease; the broker's single-active-consumer and
per-message ack are their fences.

## Workers task set

The Workers root TaskGroup contains exactly: `workers-probe` (loopback
health/readiness/metrics), the News consumer tasks when News is enabled
(`news-receiver`, `news-recovery`, `news-deduper`, `news-triage`,
`news-deliverer`, `news-janitor`), and `workers-control` (singleton lock,
heartbeat, runtime row). There is no acquisition clock, projection
coordinator, model arbiter, periodic market poll, stream ingester, identity
backfill, or universe sync task.

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
(`WorkerDatabase.run_news`) so background backlog never starves a live Event;
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
Existence on a venue is deliberately not a condition: #75 shipped that filter
behind a flag and the dry-run killed it — every tag the provider had itself
mapped to a venue was already listed, and the ones it would have removed were
real equities with no crypto perp (#89). The instrument universe labels a tag
instead: `asset_class` is `equity_or_commodity` when a grounded symbol resolves
to an `equity`/`commodity`/`index`/`fx`/`pre_ipo` instrument, `crypto` when it
resolves to a coin, and falls back to the provider's `XYZ-` prefix when the
universe is empty or does not know the symbol. Equities with no crypto perp
(`UWMC`, `TLX`) therefore still read as `crypto`; closing that is #91.
The Gate does not decide relevance: every Item is a `candidate` unless it is a
recovery replay, a law-firm template notice (strong template phrases always;
weak ones only without a grounded asset), an under-80 market-telemetry frame,
or — behind `news.gate.suppress_low_signal`, default off — an ungrounded,
non-macro social post under 70. A `listing` frame takes the
`listing_deterministic` admission, which is admitted and judged like a
candidate (#72). A member that
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
never retrieves: the Deduper computes `event_status` (storyline window facts),
the consumer adds the **told ledger** — the cards the reader actually received
in the last 4 h (`repository.told_ledger`: newest push/escalate verdicts whose
first card was not terminalised, no degraded fallbacks, plus the preliminary
storyline's own newest cards fetched separately; at most 12 entries in the
status bar, up to six same-storyline slots reserved, the rest the newest
cross-storyline cards, each with index `i`, age, magnitude, direction,
`headline_zh`) — and passes both last in the human message as the status
bar. The byte-frozen system
prompt is English (instructions) and every text field the verdict returns is
Chinese: `headline_zh` (the card header — a complete headline that keeps the
decisive fact, not a stub), `why_zh` (the one card sentence adding what the
headline does not say), a
console-only `title_zh` (the faithful Chinese title), and an `audience`
(crypto / us_equity / macro / none). The verdict also carries `novelty`
(`new_fact` / `progression` / `restatement`, judged against the told ledger)
and `restates` (the ledger index a restatement points at; -1 otherwise) —
the reader-facing memory Triage has (issue #61): dedup is byte/word-level,
novelty is the semantic last line against the same fact told again from
another outlet or under another storyline key. Magnitude and `actionable`
are calibrated in the prompt (its magnitude scale, the `actionable` definition
and the classification examples), never in code; prompt v8 files a listed
company's or token issuer's own product update at magnitude 2 and defines
`actionable` because the `model_push_actionable` branch of `decide()` requires
it (the other push paths do not check it). `decide()` owns the final
decision under a `DecidePolicy` whose defaults are the live policy and whose
values come from `news.policy`: mute -> drop; noise -> drop; a *grounded*
restatement (the model cites a ledger entry it was shown and the direction did
not flip against it; switch `restatement_drop`) -> drop (`restatement`);
magnitude >= 3 with a direction or macro scope -> escalate; high priority +
push -> escalate; model push/escalate intent, actionable, magnitude >=
`min_push_magnitude` (1) and a direction -> push (`model_push_actionable`);
unclear direction with a clear event type (product, listing, delisting,
regulation, hack, exploit, partnership, filing) at magnitude >= 2 -> push
(`unclear_but_clear_event`); other unclear -> drop; watchlist primary at
magnitude >= 1 -> push; else drop (`below_threshold`). Storyline throttling
(switch `storyline_throttle`) keeps the window-max + direction-flip rule for
`asset:` keys and caps `theme:`/`macro:` keys at `theme_cap_4h` (3) pushes
per 4 h unless magnitude exceeds the window max or the direction flips. What
gets past that soft throttle is *measured*, not claimed (policy v5, issue #81):
the card's `headline_zh` is compared with every card the reader received in the
window, and it is released as `distinct_bypass` when the closest resemblance
(character-bigram Jaccard, `tracefold.news.similarity`) stays under
`similarity_max` (0.25). At or above it the reader already has this and the key
gains a `:seen` suffix; a degraded rule-baseline verdict carries a placeholder
headline and is never released; `similarity_max = 0` restores the pre-v5 count
cap. Counts survive only as a flood ceiling (`distinct_asset_cap_2h` 6 pushes
in the last 2 h, `distinct_hard_cap_4h` 18 in the last 4 h; `throttled_by =
storyline:<key>:hard<N>` beyond it) — one storyline cannot spend an unbounded
stream on the reader however distinct each card is. This retired
`novel_bypass`, which released a card on the model's own unverified claim that
its event was new and was the last path where a self-report opened a gate;
`novelty` is still read, but only to *withhold* a grounded restatement. The
hourly cap (switch
`hourly_cap_enabled`, `news.push.hourly_cap`, default 30) throttles pushes
only. Every path names its rule; nothing drops silently. A fast retryable
model failure (timeout, rate limit, connection) or an unusable answer that is
not a `max_tokens` truncation (empty tool call, missing field) earns one more
attempt inside the deadline, and once that budget is spent a verdict that is
complete except for `novelty` is accepted as `new_fact` (`novelty_defaulted`,
prompt-v5 quality) rather than dropped on rules; model failure is degraded,
not silent:
`rule_baseline` (watchlist primary, score >= 80 with a grounded asset, or —
since #81 — a high-priority Event or a deterministic exchange notice, which is
what a missile strike, a rate decision or a delisting looks like without a
ticker) still pushes on the wire headline, everything else drops with
`degraded=true`, and three
consecutive transport failures open a 60-second circuit that also opens a
`triage_circuit_open` incident (closed by the next success); an output failure
(`news_triage_output_truncated` when the tool call hit `max_tokens`,
`news_triage_output_invalid` on a schema mismatch) is degraded but never
counts toward the circuit and records `finish_reason`, `output_tokens`, and
`parsing_error` in the trace. After the model call the consumer decides and
persists in one transaction under a per-storyline advisory lock on the final
key (`repository.lock_storyline`; `pg_advisory_xact_lock('NEWS', hashtext(key))`),
re-reading the window facts inside the lock so two same-key Events in flight
cannot both pass the throttle (the lock raises the lane's 250 ms
`lock_timeout` for that transaction only); when a card the model was not
shown has landed in the ledger by then (compared by event id, not by clock —
verdict rows carry their handler's start stamp), the consumer reloads window
facts, control and hourly count under a fresh stamp and asks the model once
more with the fresh ledger (`reasked_after_told_change`) instead of pushing a
restatement the reader just received; if that second call fails, the model's
first judgment is persisted (`reask_failed`), never the rule baseline. `news_verdicts` stores `model_decision`, `rule_baseline_decision`,
`final_decision`, `override_rule`, `throttled_by`, `degraded`, and a
replayable trace (latency, tokens, model attempts, prompt sha, input sha, the
preliminary storyline key, the preliminary and final status-bar snapshots,
the told ledger as shown with event ids, `told_count`, `restates_event_id`,
`first_verdict`/`first_input_sha256`/`reask_failed` when re-asked,
`novelty_defaulted`, the final storyline key).

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
reader contract (`news_delivery_card_v9`): the header is `headline_zh` (⚡ when
the decision is escalate; it falls back to `title_zh`, then the original
title), the first body line is `why_zh`, and the second is the facts in plain
words — direction label, magnitude label, the tickers the model called primary
and the Gate grounded, source（N 条报道）, and the leader item's publication
time in the reader's zone (UTC+8) — followed by a 打开来源 button and a small
`Tracefold · <event_id[:8]>` note. There is no original headline line, no
translated title, no event type or scope enum, no provider score, and no line
labelled as AI: those internals stay in the console and `tracefold news why`.
A degraded Event (the model chain failed and the rule baseline still pushes)
gets the wire text instead of a verdict view: the original headline as header,
the original description as the body line, and a facts line of tickers,
source and time only — no direction or magnitude the model never judged and
no "模型不可用" copy; the degraded verdict's `headline_zh` is the wire headline
too, so the console feed and the context line name the Event (issue #65).
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

Migration history is squashed. `20260818_0275_baseline` is the single root
revision: it executes the frozen `current_schema_20260818_0275.sql` dump
(every table, index, constraint, seed row of the schema as it stood after the
News V3 hard cut and Radar removal) plus `runtime_roles.sql`, and it is
irreversible. Three chained revisions follow. `20260818_0276_review_49_hard_cut`
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
of the dropped queues. `20260819_0278_macro_lane_removal` drops the whole Macro
lane: the ten `macro_*` fact/derived/queue/frontier tables, the four general
market observation tables (`market_instruments`, `market_observations`,
`market_settlements`, `market_position_facts`), the durable queue
terminal-evidence table (`queue_terminal_events`, whose only writers were the
Macro repository and the projection frontier), and the
`reject_macro_fact_mutation()` trigger function. No chained revision has a
downgrade. Earlier hard cuts live only in git history; a fresh database and a
database upgraded through the chain reach byte-identical schemas.

See [Public Contracts](CONTRACTS.md), [Operations](OPERATIONS.md), and
[Frontend Architecture](FRONTEND.md) for the other current authority surfaces.
