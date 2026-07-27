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
- news: immutable acquisition observations in `news_feed_observations` and
  idempotent normalized current `news_items`;
- macro: revision-preserving `macro_series_facts`, `macro_release_facts`, and
  `macro_documents`;
- notifications: `notifications` and the external delivery facts in
  `notification_deliveries`. `account_token_alerts` remains a Market fact that
  Notifications consumes through an explicit input.

Current read models are `token_radar_current_rows`, `token_profile_current`,
`market_tick_current`, deterministic `news_items`, `news_stories`, and
`news_story_members`, plus the six stable rows in `macro_module_current`.
Each uses stable product/window/target identity, has exactly one runtime
writer, is rebuildable from facts, and writes zero serving rows when its
business payload is unchanged.

Source configuration/fetch health in `news_sources`, queues, leases, retries,
fetch attempts, sync runs, terminal events, and agent checkpoints are control
or audit state. They are not alternate business truth.
`macro_research_publications` and `news_brief_publications` are immutable
derived research keyed by frozen evidence; they are not material facts.

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
  sources.py        frozen WorldMonitor-derived source catalog plus crypto/6551
  classification.py deterministic keyword threat/category classifier
  identity.py       WorldMonitor-compatible 512-dimension title clustering
  ranking.py        55/20/15/10 importance and Top-8 selection
  brief.py          Chinese Brief fingerprint and citation index lock
  repository.py     PostgreSQL observation, item, Story, and Brief state
  interface.py      sole external News read interface
  workers.py        the three bounded News workers

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
- News observation/item persistence plus deterministic Story
  membership/projection;
- immutable World Brief publication plus completion of its exact
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

News is a direct PostgreSQL-backed adaptation of WorldMonitor commit
`f73de5b7`, not an independent editorial ontology:

```text
96 frozen RSS/RSSHub sources
  -> conditional fetch (ETag / Last-Modified, first five entries)
  -> immutable FeedObservation before admission
  -> idempotent NewsItem
  -> full 96-hour WorldMonitor title clustering
  -> persistent Story identity + members + aliases
  -> category groups, each capped at 20 Stories
  -> deterministic Top-8 Brief selection
  -> one Chinese World Brief publication
  -> /api/news/feed + /api/news/stories/{story_id}
     + /api/news/brief + /api/news/sources
```

`news_pipeline` is the only NewsItem/Story writer. It synchronizes the
code-owned source catalog, claims due sources, performs concurrent provider I/O
outside transactions, records one FetchReceipt per attempt, and commits each
source independently. Every parsed entry first becomes an immutable
`news_feed_observations` row. Missing title, non-HTTP URL, missing date, and
future time beyond one hour remain auditable rejected observations and never
become NewsItems. Valid entries older than 96 hours are persisted as
historical inactive NewsItems so acquisition loss stays distinguishable from
active-cluster eligibility.

NewsItem identity is `(source_id, source_item_key)`, where the source item key
comes from GUID and canonical URL. Tracking parameters are removed. The
content fingerprint covers canonical URL, normalized title, and description,
but deliberately excludes `pubDate`: a source timestamp drift produces a new
observation and zero NewsItem or Story writes.

Story clustering is the Python port of WorldMonitor's
`shared/story-identity.js`: normalized titles, deterministic signed FNV-1a
512-dimensional vectors, uniform and boosted cosine channels, threshold
`0.615`, exact-duplicate union, high-containment rescue, 250-item candidate
buckets, and deterministic union-find. The entire active 96-hour set is
reclustered per pipeline cycle. Existing member ownership and seven-day title
aliases preserve a stable Story ID across representative changes; dropped
Stories are archived, not treated as current. There is no embedding,
translation, full-article extraction, browser clustering, revision product, or
per-Story AI analysis.

Threat level and category use WorldMonitor's deterministic keyword classifier,
including exclusions and historical downgrade. `news_ai_classify` is the only
optional enhancement seam, is disabled by default, and cannot gate News facts
or serving. Importance uses WorldMonitor's 55% severity, 20% source tier,
15% independent reporting-origin corroboration, and 10% recency, followed by
the narrow diplomacy/flashpoint and entity-corroboration boosts. Because
Tracefold persists this otherwise request-time score, recency uses the
equivalent one-hour healthy-cache epoch; unchanged input within an epoch writes
zero serving rows. The API exposes the factor breakdown. Global clustering
precedes per-category Top-20 truncation.

`news_world_brief` selects at most eight Stories, caps one structured
`reporting_origin` at three across all distribution feeds, and excludes
opinion, feel-good, and ephemeral live coverage.
Its fingerprint binds the ordered Story state and the prompt/workflow/schema/
locale contract. An unchanged fingerprint makes no model call and no write.
The provider chain gets one bounded attempt per configured provider under one
60-second total budget. Output is Chinese, each line is index-locked to its
selected Story, malformed lines receive a deterministic local fallback, and an
empty set or provider failure preserves the last-known-good publication.
Publication history is immutable; degraded attempts are retained for audit but
cannot replace last-known-good. `news_brief_current` is the single current
pointer and the read contract exposes `fresh`, `updating`, `stale`,
`unavailable`, or `failed` honestly.

The complete live News storage boundary is exactly ten tables:
`news_sources`, `news_source_fetches`, `news_feed_observations`,
`news_items`, `news_stories`, `news_story_members`,
`news_story_aliases`, `news_ai_classification_cache`,
`news_brief_publications`, and `news_brief_current`. Migration
`20260727_0204` destructively drops every prior `news_*` table before
creating this schema and has no downgrade or compatibility lane.

### Macro

```text
code-owned Dataset Registry + Coverage Manifest
  -> one of six clock families
  -> macro_acquisition_targets claim
  -> free official / exchange / disclosed proxy adapter
  -> typed append-only Market or Macro fact + source receipt + cursor
  -> macro_projection
  -> six macro_module_current rows
  -> persisted-only overview and module reads

official FOMC / speech body + effective-dated role fact
  -> macro_document_analysis_jobs claim
  -> immutable evidence-bound document analysis
  -> institutional stance + officials communication distribution

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
criticality, and module membership in code. The separate Coverage Manifest
declares every expected capability, including unimplemented free sources and
licensed-unavailable sources; omitting a Dataset cannot make coverage green.
Operator config only enables source families and sets runtime
cadence/lease/timeout knobs. Coverage (`complete`, `partial`,
`licensed_unavailable`), Data Health (`current`, `delayed`, `stale`, `invalid`,
`backfilling`, `unavailable`), and Judgment (`current`, `missing`, `blocked`)
are independent decision metadata, not a generic process-readiness gate.

The six product modules are `rates_fed`, `economy_inflation`,
`liquidity_funding`, `credit`, `volatility`, and `cross_asset`. Each has one
explicit v2 payload; no generic chart-array contract survives. The Calculation
Registry records every feature's inputs, formula version, windows, minimum
observations, units, gap policy, freshness, baseline, and output shape.
Treasury shape, matched breakevens, normalized asset returns, credit ladder
history, and funding comparisons remain deterministic. Credit exposes spread,
funding cost, bank supply, quality, and market-liquidity dimensions
concurrently and never reduces them to a score.

The daily judgment fixes six macro dimensions and SPY/QQQ/IWM/TLT/IEF/LQD/HYG/
UUP/GLD/USO/BTC/VIX directions to one cutoff-bounded Evidence Pack. DeepAgents
receives that exact Evidence Pack later in the completed-session research lane;
the reviewer disposition is `pass`, `revise`, or `block`. A model failure
cannot hide the six deterministic modules or the daily judgment. PostgreSQL
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
