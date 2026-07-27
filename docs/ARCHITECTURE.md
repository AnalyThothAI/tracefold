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
- market: `market_ticks`, `enriched_events`;
- news: idempotent normalized Article facts in `news_articles`;
- macro: `macro_observations`;
- notifications: `notifications` and the external delivery facts in
  `notification_deliveries`. `account_token_alerts` remains a Market fact that
  Notifications consumes through an explicit input.

Current read models are `token_radar_current_rows`, `token_profile_current`,
`market_tick_current`, and deterministic `news_stories` plus
`news_story_memberships`. Each uses stable product/window/target
identity, has exactly one runtime writer, is rebuildable from facts, and writes
zero serving rows when its business payload is unchanged.

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
  observations/  provider fact import and live evidence reads
  research/      completed-session immutable research lifecycle

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
profile, price, Radar, and collector tables; News owns `news_*`; Macro owns
`macro_*`; Notifications owns `notification*`. Platform owns Alembic,
checkpoint, and generic terminal-evidence tables. Macro has no live or hidden
dependency on News. The architecture gate checks SQL table references against
the generated current schema.

## Transaction ownership

Application services and workers own transaction scope. Repository writes use
the supplied connection and never expose commit switches or open hidden
transactions.

Important atomic units are:

- fact persistence, identity resolution, market capture, and downstream dirty
  target creation;
- current read-model write plus acknowledgement of the exact claim;
- News Article persistence plus deterministic Story membership/projection;
- immutable Story analysis publication plus completion of its exact
  evidence/model/version attempt;
- immutable Macro publication plus transition of its run to `published`;
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
macro_sync_windows
  -> provider bundles
  -> macro_observations
  -> persisted-only live evidence reads

completed-session macro_research_runs
  -> one frozen-scope DeepAgents graph
  -> one immutable macro_research_publications row
  -> persisted-only research read
```

Live Macro evidence reads bounded `macro_observations` directly through six
descriptive lenses. It has no projection table or semantic readiness gate.
Completed-session research freezes session, cutoff, and evidence visibility
before model work. PostgreSQL checkpoints are resumable execution state, not
facts or a second publication source. Read requests never invoke the graph.

## Safety boundary

`events.raw_json` and `events.event_json` remain because historical events do
not yet have a proven one-to-one `raw_frames` source edge and locator. They may
be removed only after new writes persist the edge, historical coverage is
verified at 100%, and ambiguous payloads are exported as immutable evidence.
No runtime fallback path should be introduced meanwhile.

See [Public Contracts](CONTRACTS.md), [Operations](OPERATIONS.md), and
[Frontend Architecture](FRONTEND.md) for the other current authority surfaces.
