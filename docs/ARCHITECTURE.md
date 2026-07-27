# Architecture

Tracefold is one Python service, one CLI, one React console, and one PostgreSQL
database. The architecture remains Kappa/CQRS: append-oriented material facts
are the only business truth; deterministic current views and immutable research
publications are derived state.

## Data flow

```text
providers / public streams
  -> integrations
  -> PostgreSQL material facts
  -> durable dirty targets or bounded catch-up
  -> single-writer read models or immutable publications
  -> HTTP / WebSocket / CLI / React
```

Workers recover exclusively by re-reading PostgreSQL on bounded
`interval_seconds` loops. There is no database wake plane or in-memory
correctness dependency. Provider raw frames remain inputs until normalized and
persisted as material facts.

## Truth, control state, and derived state

Material facts include:

- evidence: `raw_frames`, `events`, `event_entities`;
- identity: `token_evidence`, `token_intents`,
  `token_intent_lookup_keys`, `token_intent_resolutions`,
  `registry_assets`, `asset_identity_evidence`, `asset_identity_current`;
- market: `market_ticks`, `enriched_events`, `market_observations`,
  `market_settlements`, and `market_position_facts`;
- news: idempotent normalized Article facts in `news_articles`;
- macro: revision-preserving `macro_series_facts`, `macro_release_facts`, and
  `macro_documents`;
- notifications: `notifications` and the external delivery facts in
  `notification_deliveries`. `account_token_alerts` remains a Market fact that
  Notifications consumes through an explicit input.

Current read models are `token_radar_current_rows`, `token_profile_current`,
`market_tick_current`, deterministic `news_stories` plus
`news_story_memberships`, and the six stable rows in `macro_module_current`.
Each uses stable product/window/target identity, has exactly one runtime
writer, is rebuildable from facts, and writes zero serving rows when its
business payload is unchanged.

Source configuration/fetch health in `news_sources`, queues, leases, retries,
fetch attempts, sync runs, terminal events, and agent checkpoints are control
or audit state. They are not alternate business truth.
`macro_research_publications`, `news_brief_publications`, and
`news_story_analysis_publications` are immutable derived
research keyed by frozen evidence; they are not material facts.

## Package map

```text
tracefold.market
  capture/       provider-neutral evidence ingestion
  identity/      token and asset identity resolution
  pricing/       append-only market facts and current prices
  profiles/      source-backed token profiles and image state
  radar/         transparent factor projection
  views/         persisted market read queries

tracefold.news
  identity.py    Article identity, provenance, Story matching and projection
  repository.py PostgreSQL facts, membership, lifecycle and analysis publication
  interface.py  sole external Story read interface
  workers.py    bounded RSS ingest and Story-analysis workers

tracefold.macro
  registry.py    code-owned Dataset Registry and six-module membership
  acquisition.py clock-driven claim, provider-I/O, receipt, fact and cursor flow
  calculations.py versioned calculation registry and transparent features
  projection.py  six current decision modules
  judgment.py    08:50 New York Evidence Pack and deterministic daily judgment
  research/      Evidence-Pack-bound immutable DeepAgents research lifecycle

tracefold.notifications
  durable notification creation, rules, and delivery state

tracefold.integrations
  provider and external-system adapters, including DeepAgents

tracefold.platform
  config, PostgreSQL/Alembic, telemetry, paths, and generic worker mechanics

tracefold.app
  composition, repositories/providers, worker registry, HTTP/WS, and CLI
```

The four business package roots are their public Python interfaces:
`tracefold.market`, `tracefold.news`, `tracefold.macro`, and
`tracefold.notifications`. Consumers outside an owning package import from the
root only. Internal subpackages may change without creating a repository-wide
import graph.

The dependency direction is:

```text
app -> integrations + business packages + platform
integrations -> business package interfaces + platform
news -> platform
macro -> market + platform
market -> platform
notifications -> platform
platform -> Python / third-party libraries only
```

Business packages never import `tracefold.app` or provider integrations.
Transport adapters do not own business rules. Generic worker mechanics live in
`tracefold.platform.workers`; queue state machines and read-model behavior stay
with their business owner. These rules are executable in
`tests/architecture/test_backend_boundaries.py`.

SQL ownership follows the same boundary: Market owns the event, token, asset,
profile, price, Radar, collector, general cross-asset observation, and
settlement tables; News owns `news_*`; Macro owns `macro_*`; Notifications owns
`notification*`. Platform owns Alembic, checkpoint, and generic
terminal-evidence tables. Macro imports Market only through
`tracefold.market`, has no live or hidden dependency on News, and never
duplicates general market facts into Macro storage. The architecture gate
checks SQL table references against the generated current schema.

## Transaction ownership

Application services and workers own transaction scope. Repository writes use
the supplied connection and never expose commit switches or open hidden
transactions.

Important atomic units are:

- fact persistence, identity resolution, market capture, and downstream dirty
  target creation;
- one Macro acquisition completion: normalized fact insert, append-only source
  receipt, cursor advance, and compare-and-set target completion;
- current read-model write plus acknowledgement of the exact claim;
- News Article persistence plus deterministic Story membership/projection;
- immutable Story analysis publication plus completion of its exact
  evidence/model/version attempt;
- immutable Macro Evidence Pack, daily judgment, or research publication plus
  the corresponding stable session transition;
- notification creation plus activation of delivery rows;
- retry or terminal transition plus mutation of its source queue row.

Provider, model, subprocess, filesystem, and network I/O occurs outside
database transactions. External delivery follows claim, commit, I/O, then a
compare-and-set completion or retry.

## Product flows

### Market and Token Radar

`market_tick_current` is transactionally maintained from append-only
`market_ticks`; it has no projection worker or dirty queue. Explicit bounded
fact replay rebuilds it.

```text
events + intents + resolutions + market facts
  -> token_radar_dirty_targets
  -> source edges + target features
  -> token_radar_current_rows + publication state
  -> Radar, Search, Token Case, notifications
```

The public Radar row is a transparent `factor_snapshot` built only from
persisted identity, social, and market facts.

### News

```text
configured sources
  -> internal RSS/RSSHub adapter
  -> fetch receipts + observations + immutable Article revisions
  -> versioned Article identity features v2
  -> multi-channel candidate recall
  -> constraint-first proof-ladder Event Story admission
  -> single-writer deterministic Story memberships + profiles + material events
  -> Latest / Priority Story views
  -> Narrative grouping + Brief Selection + Proposal
  -> immutable Brief Activation + singleton Active pointer
  -> content-addressed validated AI Publication
  -> Story Interface -> HTTP + React
```

Article identity is publisher-artifact scoped and deterministic. Story identity
v2 first recalls bounded candidates through content fingerprint, normalized
exact title, high title containment, same-language lexical similarity,
deterministic event anchors, and named events. Admission is a separate ordered
proof ladder. Place, actor direction, action/state transition, event object,
policy stage, named event, temporal episode, and explicitly
identity-defining-number conflicts veto every positive proof. Exact title,
near-complete containment, high same-language member similarity, deterministic
event anchors, and the stricter cross-language anchor path are then evaluated
in order. Runner-up ambiguity creates a separate Story. There is no union-find,
embedding, translation, LLM, or browser clustering path.
Every admitted FeedObservation either introduces an ArticleRevision or resolves
by exact content to an existing Revision of the same publisher artifact. A
repeat Observation remains immutable acquisition evidence but does not invent a
material Revision. Revision projection and MaterialEvent closure share the
total order `(observed_at_ms, revision_id)` so equal-millisecond arrivals cannot
make event hashes depend on unrelated deterministic IDs.
The frozen production-derived and WorldMonitor-reference evaluation, red/green
metrics, release floors, and remaining limits are owned by
`docs/DEVELOPMENT.md`; the executable labels live in
`tests/fixtures/news_story_identity_golden.json`.

Source role, trust, acquisition chain, publisher organization, and reporting
origin remain separate so syndication cannot manufacture corroboration.
Lifecycle, Impact, Priority, evidence posture, and Brief eligibility are
deterministic scheduled projection state. `latest` and `priority` are two
ordered views over the same Story identities; they never create a second
projection.

Global Brief separates content identity from temporal currentness:

- `news_brief_selections` stores the content-addressed deterministic editorial
  portfolio and exact synthesis input hash.
- `news_brief_proposals` stores the one candidate transition being observed
  through ordinary, verified-critical, or rectification debounce.
- `news_brief_activations` records each immutable transition that became
  current; `news_brief_active` points to exactly one Activation.
- `news_brief_publications` stores immutable AI analysis by synthesis input plus
  the qualified model/prompt/workflow/schema/locale contract.
- `news_ai_current_targets` is the transactionally replaced intent for the
  evidence and qualified contract that may currently attach for each Activation
  or Story; an older in-flight result can enter immutable history but cannot
  regain the current pointer.
- `news_brief_activation_analysis` attaches a generated or exactly reused
  Publication without allowing AI to advance Active. Each Activation has at
  most one unsuperseded attachment: requesting a different qualified
  model/prompt/workflow/schema/locale contract first withdraws the incompatible
  current attachment, so the public state is honestly pending or failed until
  the new contract publishes or an exact cached Publication is reattached.

Consequently A → A is a no-op, while A → B → A produces A₁, B₁, and A₂ even
when A₂ reuses A₁'s Publication. Activation time, evidence cutoff, Publication
time, and attachment time are distinct. A late or invalid model result can be
retained as immutable history/cache but cannot overwrite a newer Activation.
Superseded attachments likewise remain immutable provenance and cache, never
the current analysis.
A pending or failed model never hides deterministic Story cards.

### Macro

```text
code-owned Dataset Registry
  -> one of six clock families
  -> macro_acquisition_targets claim
  -> free official / exchange / disclosed proxy adapter
  -> typed append-only Market or Macro fact + source receipt + cursor
  -> macro_projection
  -> six macro_module_current rows
  -> persisted-only overview and module reads

08:50 America/New_York trading session
  -> cutoff-bounded six-module compilation
  -> immutable macro_evidence_packs
  -> immutable macro_daily_judgments

completed-session macro_research_runs bound to that Evidence Pack
  -> one checkpointed DeepAgents graph and reviewer
  -> one immutable macro_research_publications row
  -> persisted-only research read
```

The acquisition clock families are `daily_settlement`, `scheduled_release`,
`official_state`, `official_document`, and explicit `backfill`. They are
separate workers over one target table, not a uniform
bundle poller. Claims use `SKIP LOCKED`; provider I/O occurs outside database
transactions; completion atomically writes facts, receipt, cursor, and target
state. Unchanged source content writes zero fact rows while every attempt
retains a receipt. Revisions append a new fact and never overwrite history.

The Dataset Registry fixes ownership, clock, adapter, trust tier, freshness,
criticality, and module membership in code. Operator config only enables source
families and sets runtime cadence/lease/timeout knobs. Dataset and module
quality are explicit (`current`, `delayed`, `stale`, `backfilling`,
`unavailable`; `ready`, `degraded`, `blocked`) and are decision metadata, not a
generic process-readiness gate.

The six product modules are `rates_fed`, `economy_inflation`,
`liquidity_funding`, `credit`, `volatility`, and `cross_asset`. The
Calculation Registry records every feature's inputs, formula version, windows,
minimum observations, units, gap policy, freshness, baseline, and output
shape. The daily judgment fixes six macro dimensions and SPY/TLT/HYG/DXY/GLD/
USO/BTC/VIX directions to one cutoff-bounded Evidence Pack. DeepAgents receives
that exact Evidence Pack later in the completed-session research lane; the
reviewer disposition is `pass`, `revise`, or `block`. A model failure cannot
hide the six deterministic modules or the daily judgment. PostgreSQL
checkpoints are resumable execution state, not facts or a second publication
source. Read requests never invoke providers or the graph.

## Safety boundary

`events.raw_json` and `events.event_json` remain because historical events do
not yet have a proven one-to-one `raw_frames` source edge and locator. They may
be removed only after new writes persist the edge, historical coverage is
verified at 100%, and ambiguous payloads are exported as immutable evidence.
No runtime fallback path should be introduced meanwhile.

See [Public Contracts](CONTRACTS.md), [Operations](OPERATIONS.md), and
[Frontend Architecture](FRONTEND.md) for the other current authority surfaces.
