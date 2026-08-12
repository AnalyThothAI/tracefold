# Public Contracts

Tracefold exposes one configuration contract, one HTTP/WebSocket service, and one CLI. This document records stable behavior; generated OpenAPI is authoritative for exact HTTP fields.

There are no compatibility aliases for retired products, tables, worker names, routes, or response fields. A behavior change updates source, tests, generated contracts, and this document in the same change.

## Runtime configuration

The active operator-owned application file is
`~/.tracefold/config.yaml`. It contains deployment/domain choices,
PostgreSQL role DSNs and password-file references, providers, credentials,
API, and public WebSocket settings. Worker topology, cadence, deadlines,
resource limits, batches, leases, retries, and model reservations are
code-owned and are not configuration fields.

Repository fixtures, `.env` files, and generated docs are not runtime
configuration. No static example is a second schema authority: the
`tracefold init` command generates the default directly from the typed settings
implementation.
`uv run tracefold config` reports the effective paths and redacted settings.
Unknown settings or worker keys fail validation.

`tracefold init` creates the operator directory, config, cache/log directories,
and bootstrap/Serve/Workers/migrate password files. The operator directory is
mode `0700`; config and password files are `0600`. A normal rerun preserves
existing config and password contents while repairing permissions.
`tracefold init --force` replaces only `config.yaml`; it does not rotate
existing database passwords. The generated config has a new WebSocket token
but no live provider/model/webhook credential, and `news.push.enabled` is
false. `news.rss_enabled` is also false.

The configuration schema uses typed nested models directly
(`storage.postgres`, `api`, `llm`, `gmgn`, `providers.*`, and `upstream`).
Root-level `postgres_*`, `api_*`, provider, LLM, and upstream forwarding
aliases are not part of the configuration contract.

Fresh configs subscribe GMGN to `sol`, `eth`, `base`, `bsc`, and `robinhood`.
The default OKX DEX discovery/quote set includes chain index `4663`, whose
canonical Tracefold chain ID is `robinhood`; no runtime alias or inferred
fallback supplies that mapping.

Top-level `handles`, top-level `notifications`, and `news.sources` are retired
inputs. Any equivalent retired key fails
validation; there is no alias, merge, or generated-source fallback.

`llm.api_key`, `llm.base_url`, and `llm.news_brief_model` are one direct
DeepSeek configuration. They are all absent or all present; a partial triple
fails validation, and Tracefold never supplies an implicit endpoint or model.
The public News Brief waterfall is the code-owned Ollama endpoint/model, this
configured direct DeepSeek slot, then optional Groq when
`llm.groq_api_key` is present. News Push title translation reuses exactly the
same direct triple and has no provider fallback. Model execution policy,
provider order, Groq endpoint/model, timeouts, token budgets, cadence, leases,
retries, and reservations are code-owned.
Environment variables are not a credential contract.

`news.opennews_token` is the operator-owned secret for the production News
source. It is reported only as the redacted boolean
`news.opennews_token_configured`. When absent, News reports
`opennews_token_missing`; no substitute credential path is used.

`news.rss_enabled` is the sole public RSS activation switch and defaults to
`false`. CLI diagnostics report the effective boolean. There is no source list,
nested RSS configuration, environment override, or compatibility alias.

`news.push` contains only `enabled`, `feishu_webhook_url`, and optional
`feishu_signing_secret`. Enabling push requires the webhook, which must be the
supported Feishu HTTPS custom-bot v2 boundary. If a non-empty signing secret is
present, delivery includes the computed `timestamp` and `sign`; if absent,
delivery is unsigned and includes neither field. Optional title translation
reuses the direct DeepSeek `llm.api_key`, `llm.base_url`, and
`llm.news_brief_model`; there is no `news.push.translation` block, second
credential, inferred endpoint, or fallback provider. CLI diagnostics expose only configured
booleans for Feishu and translation: `translation_enabled` derives from Push
enablement plus the global LLM credential, while `translation_configured`
reports that global credential availability. Each frozen internal delivery envelope
contains the non-secret `auth_mode` (`signed` or `unsigned`) so a retry cannot
change modes; it never contains the webhook, secret, timestamp, or signature.
Threshold, cadence, deadlines, retries, translation target, 7.5-second request
timeout, 8-second total translation budget, 500-grapheme input ceiling,
validation, and card policy are code-owned. This Push translation remains an
outbound presentation adapter and does not enter the serial model arbiter. For one fresh
non-Chinese title it sends only that title in at most one request to the
configured direct DeepSeek provider and never switches providers or retries. After
resource admission, a durable `attempted` fence is committed immediately before
submission. If the process stops before the outcome is frozen, recovery sends
the original-title fallback (or suppresses it when stale) without another model
request; this uncertain dispatch is conservatively counted as an interrupted
attempt. The
Feishu JSON 2.0 card uses a valid Chinese translation as its plain-text header
and shows the original visibly in the body. Chinese input bypasses translation;
failure or overlong input uses the original header and a visible fallback note. Its
live-alert admission requires both the selected Item's provider article clock
and its dedicated provider score/assets eligibility-evidence clock to be newer than the
first-enable baseline and the article to be no more than 15 minutes old; this
prevents a future-skewed provider clock from replaying baseline evidence. Stale recovery data is
recorded as suppressed and is never sent. Every frozen retry rechecks that age
immediately before network submission and becomes suppressed once the deadline
passes. The selected Item must also contain at least one non-empty
provider-labelled asset symbol; a normalized symbol set contained entirely in
`CL` and `XYZ-CL` is excluded as CL-family-only noise. Mixed sets such as `CL`
plus `BTC` remain eligible. This asset qualification is Push-only and does not
hide the Story from News reads. Its compact body shows only the selected
highest-score Item's OpenNews asset symbols and provider score under the
generic `关联资产` label. Asset symbols preserve provider order after
case-insensitive deduplication. A canonical
HTTP(S) Item URL adds one `查看原文` button; a missing URL omits the button. The
card has no subtitle, summary, signal, grade, Story score, source, or publication
time. Push translation is presentation-only: Article, Story, provider score, and
public News read models remain unchanged.

`tracefold.app.workers.run_workers(settings)` is the sole public Workers root.
Worker topology, private due/periodic loops, the projection EDF, the serial
native-state model arbiter, and all resource capacities are code-owned.
Configuration cannot add another worker or derived product lane. Explicit
Macro backfill is a synchronous CLI maintenance action and is absent from the
steady Workers root.

## Operator lifecycle

The fresh-clone operator contract is `make up`. It preflights `uv`, Docker,
Compose, `curl`, and daemon access; runs idempotent initialization; builds the
frontend and backend image; performs fresh-volume role bootstrap; runs the
one-shot migration; starts Serve and Workers; and waits for required health and
console boundaries. A repeated invocation preserves config, passwords, and
named-volume data.

`make status` fails non-zero when PostgreSQL, migration, Serve, Workers, either
runtime readiness endpoint, or console HTML is missing or unhealthy.
`make logs` follows the bounded startup services. `make down` stops the stack
without deleting the named PostgreSQL volume. These targets do not auto-hard-cut
an unknown non-empty database.

## HTTP

The service exposes `/healthz`, `/readyz`, `/metrics`, `/ws`, static frontend assets, and `/api/*`.

- `/healthz` is process liveness.
- `/readyz` combines a lightweight PostgreSQL liveness check with the cached startup schema/composition result. It does not inspect providers, queues, or business freshness.
- `/api/status` separates process/database/Workers runtime truth from Provider
  operations. `runtime` fails closed on stale worker heartbeats; `providers`
  reports configured ownership, durable circuit state, continuous-source
  freshness, and owned or unowned queue backlog without calling an upstream.
- Read endpoints do not call providers, execute models, mutate facts, or rebuild projections.

Status contains no provider/model credentials, base URLs, request policy,
capacity counters, prompt contents, or raw model responses. Code-owned
workflow/prompt versions may accompany bounded Brief run telemetry; they are
not operator configuration.

API responses use a typed envelope:

```json
{"ok": true, "data": {}}
```

Errors use `ok: false` with a stable error code. Pydantic response models generate `docs/generated/openapi.json` and `web/src/lib/types/openapi.ts`; frontend code consumes those generated types.

### Endpoint families

| Family | Routes | Source of data |
|---|---|---|
| Bootstrap/status | `/api/bootstrap`, `/api/status` | runtime composition, worker status, and persisted Provider operations |
| Events | `/api/recent`, `/api/events/by-ids` | persisted event/evidence facts |
| Search/case | `/api/search`, `/api/search/inspect`, `/api/token-case`, `/api/target-posts`, `/api/target-social-timeline` | Evidence, identity, profile, and market facts owned by those readers |
| Radar/market | `/api/token-radar`, `/api/live-market` | stable PostgreSQL current read models |
| Macro | `/api/macro/overview` and six typed module routes | persisted six-module current rows built from Macro/Market facts and Fed document analysis |
| News | `/api/news/feed`, `/api/news/stories/{story_id}`, `/api/news/brief`, `/api/news/sources`, `/api/news/status` | public WorldMonitor RSS plus OpenNews current facts, deterministic Story/selection state, and one sealed half-hour Brief current/LKG payload |
| Images | `/api/token-images/{image_id}` | ready mirrored assets under the operator cache root |

There is no CEX OI/detail product API. Generic exchange facts and provider adapters remain internal inputs to supported products.

### Token Radar

`GET /api/token-radar` reads the one `token_radar_current` current/LKG
singleton. It has no product query parameter: `window`, `venue`, `limit`,
`scope`, sorting, filtering, and pagination are rejected rather than ignored or
aliased. The existing authentication `token` query remains an authentication
transport, not a Radar option. The product is one fixed four-hour causal view:
current source time is `[t-4h, t]` and prior source time is `(t-8h, t-4h)`.
The twelve-hour input horizon seeds the adjacent windows at `t-4h` from its
first eight hours, then supplies the final four-hour replay transition to `t`;
that reconstruction is neither a selectable nor a third public window. There
is no one-hour or twenty-four-hour Radar variant.

The exact data payload is one `token_radar_snapshot_v4` object. Before the
first successful v4 sample it is:

```json
{
  "schema_version": "token_radar_snapshot_v4",
  "state": "unavailable",
  "stale_reason": null,
  "state_changed_at_ms": 0,
  "social_evidence_as_of_ms": 0,
  "eligible_total": 0,
  "items": []
}
```

`state` is exactly `current|stale|unavailable`. `current` means the latest
complete sample published successfully. `stale` preserves the complete
last-known-good Items and uses only
`source_unavailable|projection_failed` as its non-null `stale_reason`.
`unavailable` has `stale_reason=null`, zero social clock/counts, and no Items
because no v4 LKG exists. `state_changed_at_ms` changes only when this public
state/reason changes; `social_evidence_as_of_ms` is the latest persisted social
fact-availability clock represented by the replay, never a market clock.
Repeated identical current or stale observations do not advance either clock
or rewrite serving state. `schema_version` is the only public semantic version;
the Gate/ruleset version and fingerprints remain internal.

`items` contains at most fifty entries in server-owned order. Each entry has
exactly canonical `target` identity (`target_type`, `target_id`, `symbol`, and
nullable `name`, same-origin `logo_url`, `chain`, `exchange`, `address`);
`trigger_event_id`, `trigger_source_event_at_ms`, and `qualified_at_ms`;
`why_now` current/prior four-hour mention counts and their difference;
`evidence` actual independent-author count, independent-text count, time to the
required author, and duplicate share; and one nullable presentation-only
`market` packet:

```json
{
  "price_usd": null,
  "price_observed_at_ms": null,
  "price_change_since_signal": null,
  "market_cap_usd": null,
  "market_cap_observed_at_ms": null
}
```

`logo_url` is either `null` or the exact same-origin form
`/api/token-images/{64-lowercase-hex-image-id}`. Each price or market-cap value
is paired with its own observation clock; absence, staleness, future time,
non-positive value, or non-finite value nulls only that presentation fact.
`price_change_since_signal` additionally requires a valid current price and the
persisted trigger price anchor, with its current-price observation no earlier
than the trigger source-event time. Market presentation never changes admission,
qualification, or order, and v4 has no market `status` or
`counter_evidence`. `eligible_total` counts the complete eligible population
before the maximum-fifty selection; Items contain that full population when it
is at most fifty and exactly fifty otherwise. Order is `qualified_at_ms`
descending with stable `(target_type, target_id)` ties. Trigger source time
cannot exceed qualification time, qualification time cannot exceed
`social_evidence_as_of_ms`, target keys are unique, and the complete
uncompressed snapshot is capped at 96 KiB.

Target identity is a closed discriminated contract. `Asset` requires non-empty
`chain` and `address` with `exchange=null`; `CexToken` requires a non-empty
`exchange` with `chain=null` and `address=null`. Robinhood Chain uses the single
internal chain value `robinhood` (provider adapters map its external chain index
`4663` at the OKX boundary); the public payload does not publish a second
`eip155:4663` alias. When an exact-address Asset has no canonical symbol yet,
`symbol` contains that same full address as a deterministic presentation
fallback; clients present it as a contract address rather than a ticker.

The response has `Cache-Control: private, no-cache` and a strong ETag bound to
the complete served v4 object, including public state. A matching
`If-None-Match` returns `304` with no body, including for unchanged healthy or
unchanged stale reads. The endpoint never calls a provider, recalculates the
reducer, hydrates a profile, returns source-event lists, or falls back to a v3
contract. The writer obtains identity/profile, exact trigger-price anchor,
current-price, and independently fresh market-cap facts in one bounded batch
read only after Top-50 selection; the browser makes no per-Item profile or
live-market data request. Scores, ranks, decisions, factor families, per-rule
Gate audits, rejected-candidate histories, normalization, security judgments,
windows, venues, and compatibility fields do not exist. Radar v4 changes no
WebSocket route, message, replay, or subscription contract.
Search and Token Case remain independent fact readers; a Radar link may focus
the exact trigger Event in Token Case, but Radar current state is not copied
into the dossier.

`/api/stocks-radar` is removed and returns `404`; there is no compatibility
alias, redirect, or replacement Stocks contract. The retained US-equity identity
catalog is an internal collision guard for token resolution, not a public
Stocks interface.

### News

The News public surface is exactly five read-only routes:

- `GET /api/news/feed?category={category}&level={level}&source_id={source_id}&reporting_origin={reporting_origin}&provider_score_gt={score}&q={query}&sort={importance|latest}&limit={limit}&cursor={cursor}`
  returns one flat global Story page. `importance` remains the API default;
  page size remains 50 by default and is capped at 100. Omitting filters returns
  the complete materialized Story population. `provider_score_gt` is a strict
  comparison against the backend-selected maximum numeric OpenNews score;
  `reporting_origin` is a normalized exact match against the published Story
  closure. `q` is normalized server-side search over current member title and
  description, the Story-snapshot reporting origin, provider source, and asset
  symbols. All search and filters run before deterministic keyset ordering and
  pagination. Every cursor and ETag binds the complete
  normalized filter identity. The response includes Stories, filtered facets
  including reporting origins, `next_cursor`, and `has_more`.
  Source and reporting-origin facets expand deterministic dimensions stored in
  the current Story read model; they preserve the same per-Story deduplication,
  origin normalization, labels, and filtered counts without scanning member
  Item history. Provider-score filtering still evaluates current persisted
  metadata for the published Story membership closure; source/origin filters
  and facets bind to the atomic Story snapshot until its next replacement.

  Each Story carries nullable `provider_evidence`, selected by the backend from
  the member with the maximum numeric OpenNews provider score and deterministic
  publication-time/Item-ID ties. It binds that Item ID, URL, and bounded
  provider metadata together. Public provider metadata exposes the upstream
  labels as `assets`; it never exposes the provider's misleadingly named raw
  `coins` field. An asset is a provider label and may name crypto, an equity,
  or a commodity such as oil; Tracefold does not infer or correct its class.
  The browser does not cluster, score, select the maximum, classify assets, or
  reorder them.
  Each Story carries an exact `notification` object. `eligible` is the current
  Story Push qualification, and `ineligible_reason` is null when eligible or
  exactly `disabled|score_threshold|no_asset|cl_family_only|baseline|stale`.
  The reason precedence is score threshold, asset presence, CL-family-only
  noise, runtime enablement, durable first-enable baseline, then the 15-minute
  Article deadline; an Article exactly 15 minutes old is still eligible.
  `delivery_state` is independently and exclusively derived from the durable
  ledger as `not_created|pending|sent|suppressed|failed`. Thus an ineligible
  Story may still be `sent`, `suppressed`, `failed`, or `pending`; a later
  score change, missing current provider evidence, expiry, or disabled runtime
  never erases a historical delivery fact. Current membership also resolves
  the existing selected-Item ledger after a Story-ID change. The object is a
  read-time Story Push projection, not a generic Notifications product, and
  the browser does not reproduce its policy.
- `GET /api/news/stories/{story_id}` returns one current Story and its complete
  NewsItem evidence. It exposes representative/scoring item identity,
  title/reporting-origin/time, classification, reporting-origin count,
  importance score, and the transparent factor breakdown. Member and
  representative URLs are nullable for linkless dispatches. An expired Story
  ID returns not found; there is no archived Story contract, revision timeline,
  or per-Story AI analysis.
- `GET /api/news/brief` returns exactly one whole sealed public Insights
  payload or no payload. `state` is `unavailable`, `current`, `degraded`,
  or `last_known_good`; bounded current-slot telemetry remains separate.
  The payload seals its UTC half-hour slot, server-ordered `top_stories`,
  member-title/distinct-source evidence, selection-drop statistics, primary
  source-age range, L1/L2/none content, source slots, versions, validation, and
  provenance. It does not return publication history or request-time Story
  joins. A degraded run never replaces or refreshes a healthy whole LKG.
- `GET /api/news/sources` returns primary OpenNews first, followed by the
  enabled code-owned public RSS breadth/corroboration sources, with source kind, feed
  schedule/claim state, latest outcome, bounded rejection/item counts, and
  OpenNews live/overlap status. Its opaque cursor is bound to that server order.
- `GET /api/news/status` derives warming/ready/degraded News health from
  PostgreSQL source, Story-invariant, public Brief, and outbound push state. It also
  exposes the operator-facing `live`, `recovering`, or `stalled` state and the
  last successful Story publication clock. Ingest health is driven by the
  OpenNews primary lane and reports RSS breadth/corroboration
  enablement plus totals/success/failure/claims as additional evidence; Story health reports
  only projection clocks, counts, and invariant failures.

`/api/news/feed` and `/api/news/brief` emit an ETag, honor
`If-None-Match` with `304`, and use `Cache-Control: private, no-cache`.
Every read is PostgreSQL-only: it never fetches a source, calls a model,
reclusters, or repairs state.

The React `/news` primary reading modes are public Feed plus Brief, alongside
the Status and Sources evidence pages. The default `重点` Feed uses the fixed
strict `provider_score_gt=70`; URL-owned `view=all` removes that read filter.
Both keep server order, and neither adds personalization or a user-adjustable
threshold.

NewsItem identity is `(source_id, source_item_key)`. RSS identity prefers GUID,
then canonical URL, then a deterministic title/publication-time key. OpenNews
uses provider record ID as its source item key and overlap identity. OpenNews
reports upsert one canonical current fact. The title is the first non-empty logical plaintext
block, capped at 500 JavaScript UTF-16 code units; a clamp that splits a
surrogate pair is converted with Web scalar-value replacement before storage.
A cleaned explicit description wins; otherwise remaining blocks form the
description, with the 40-to-400 JavaScript UTF-16 code-unit contract,
Web scalar-value replacement after truncation, and title-equality suppression.
Verified Twitter wrapper reports use
the author handle as reporting origin; other reports use `newsType`, then the
canonical URL host, then `opennews`. Linkless reports remain valid. Provider
metadata is limited to the provider-source label, `score`, `signal`, `grade`,
and provider-labelled `assets` details. The OpenNews adapter persists the
upstream raw `coins` member as source evidence, while every public News read
maps it to `assets` and omits `coins`.
Provider annotations merge metadata into the same current row; translation
frames received from OpenNews and non-news messages are discarded. Provider
metadata is descriptive and does not affect Story identity, classification,
importance, Feed ordering, or Brief. A numeric provider score may qualify the
already projected Story for a read-time `provider_score_gt` filter or the
separate outbound push state machine; neither changes the materialized Story
population. The timestamp at which the current numeric score value changed is
persisted separately from Story-owned
`updated_at_ms` and becomes the delivery ledger's SLO clock. Story identity is
the full SHA-256 of the earliest normalized title in the selected physical
RSS/OpenNews component using WorldMonitor-compatible identity.
Story IDs identify the exact shared lexical components; `canonical_key` keeps
the caller-owned public `titleHash` used for first-stage signals. Separate
components may share that public tracking hash while retaining distinct Story
IDs and complete Item membership. The public seed stage independently applies
the pinned JavaScript UTF-16 `title.length > 10` gate before rerunning the same
clustering kernel. A short bridge can therefore split one complete Story into
multiple selector candidates that map back to the same Story ID. No additional
semantic merge rule is exposed.
The code-owned source catalog is the pinned WorldMonitor
`full/en + INTEL_SOURCES` public population: exactly 179 physical HTTPS feeds,
183 category memberships, 178 reporting-source names, and 17 categories. The
RSS/Atom parser considers the first five wire entries before any gate. A kept
entry requires a title and a parseable publication time no more than one hour
in the future; a link is optional and retained only when HTTP(S). Successful
fetches atomically replace one source snapshot. If none of those first five
entries has a title, parsing fails instead of publishing a successful empty
snapshot. Unchanged snapshots write zero NewsItem facts, failures preserve the
prior snapshot, and RSS facts expire at 96 hours. The reader follows at most two
redirects itself; the initial URL and every redirect target must be public HTTPS
and every resolved address must be globally routable before a request is sent.

Story calculation joins the primary 12-hour OpenNews facts with RSS facts
expanded in pinned category-major membership order, runs lexical clustering,
keyword classification, corroboration, and
importance before the stable top-20 cap in each category, then forms a physical
union of the capped RSS Items plus all current 12-hour OpenNews Items. The
materialized Story/member closure contains each physical Item once. The public
seed retains duplicate category memberships, so public `source_count` can
exceed `unique_source_count`; the latter is the distinct reporting-origin
count. It then applies the JavaScript UTF-16 `title.length > 10` gate,
reclusters with the same kernel, and derives second-stage
importance/admissibility, 16-hour effective recency, the maximum-three
primary-source cap, and corroborated-lead reservation. It selects at most eight
Stories in stable server order and reports admissibility, source-cap, and
overflow drops. There is no personalization, embedding, topic grouping,
entity veto, client reorder, or guaranteed topic/multi-source quota.

L1 receives only ordered primary headlines, primary sources, and distinct
source counts. Every provider response must pass the same citation-scoped
composer used for publication; rejection advances the waterfall
`Ollama llama3.1:8b -> configured direct DeepSeek -> Groq
llama-3.3-70b-versatile`. The direct slot uses the exact configured endpoint,
key, and model triple; partial configuration is rejected and no URL/model is
inferred. L1 and the corroboration-gated single-headline
L2 fallback share one 60-second budget. An L1 publication is `quality=ok` with
one indexed line and one source slot per Top Story. L2 or no-text output is
`quality=degraded`; L2 has no Story lines and at most one valid source. Empty
selection makes no model call or publication, and a non-empty selection with
no eligible lead makes no model call. That target records `brief_kind=none` and
can advance its complete degraded Top Story snapshot only when no healthy LKG
exists.

`selection_fingerprint` binds the complete current selection snapshot.
Brief persistence is exactly `news_brief_selection_current` plus
`news_brief_current`, both singleton rows. UTC half-hour slot identity is the
only run identity. Story publication does not wait for RSS bootstrap. An empty
Top Story selection is not claimable, makes no model call, and cannot complete
a slot or replace the served payload; if OpenNews or RSS later supplies an
eligible Story in the same half hour, that selection can still be frozen. A
claim freezes the complete non-empty selection in the current row, uses one
120-second fenced lease, and finalizes only that frozen selection even if live
Story selection changes meanwhile. The current slot is attempted first;
restart never replays every missed slot. `publication_id` hashes the complete
sealed served payload except its own ID. The current row also owns bounded
attempt/failure/outcome/pointer telemetry and the whole current/LKG payload.
Healthy output advances it; degraded output preserves a healthy whole LKG and
otherwise advances one complete degraded payload. There is no target
fingerprint, run table, publication table, publication history, or
compatibility reader.

`/api/news/status` exposes four independent News health layers: `ingest`,
`story`, `brief`, and `push`. Brief reports the same public state, current
slot/publication identity, and bounded latest-run telemetry. Push reports
disabled/configured, baseline,
pending/retry/terminal counts, latest explicit delivery, and bounded sanitized
error evidence without exposing secrets or card content. Its nested
`translation_24h` reports durably fenced v2 attempts, including conservatively
counted ambiguous interrupted dispatches, successes, success ratio, P95
latency, failure-code counts, and SLO result; pre-fence skips are not attempts.
Its exact `sample_complete` flag is `false` when the 24-hour population exceeds
the bounded read sample; in that state numeric fields are neutral zero/null
placeholders rather than partial metrics, status includes
`push_translation_sample_overflow`, and operators must not treat them as the
whole-window SLO. A complete empty window reports `sample_complete=true`.
`delivery_24h` reports clean v2 sent/terminal completion count, P95 from the
persisted numeric-score fact clock, completed samples whose latency exceeded
120 seconds, and the 90-second P95 SLO result.
It uses the same exact `sample_complete` semantics and reports
`push_delivery_sample_overflow` when the bounded sample does not cover the
whole 24-hour population.
Deterministic Story
cards remain readable while Brief or push is unavailable.

Production News always runs the OpenNews primary low-latency lane. The sole RSS
switch is `news.rss_enabled`; it defaults to `false`, and only `true` schedules
the exact code-owned public RSS breadth/corroboration catalog. When false,
reconciliation disables prior RSS rows and releases their claims,
`/api/news/sources` serves OpenNews only, Story uses its current OpenNews
population, and status returns `rss.enabled=false` without an RSS degradation
reason. OpenNews uses one WSS
stream for current facts. Initial connection and reconnect each trigger one
bounded REST overlap; queue overflow ends the current stream so its reconnect
uses the same single trigger. REST reads newest-first from page 1, at most
eleven 100-item pages, and stops at the provider's last page, the first provider
record persisted before the attempt, or the 12-hour cutoff. It consumes the
terminating overlap row, and an eleven-page exhaustion records its outcome
without scheduling another search. Each RSS turn claims at most one due source and persists its
conditional-fetch and bounded outcome state. There is no persisted gap flag,
boundary, version, or unbounded recovery lane. OpenNews acquisition priority
does not alter deterministic Story scoring, selector ordering, or its
reporting-origin tier.

There is no `/api/news/stories` collection, `view=latest|priority`, Brief
history route, analysis request route, item route, News WebSocket payload,
inbound/public webhook route, personalized Brief, compatibility alias, or
alternate clustering path. Feishu is a Workers-only outbound Adapter.

### Macro

Macro exposes one overview and six typed current-module reads:

```text
/api/macro/overview
/api/macro/rates-fed
/api/macro/economy-inflation
/api/macro/liquidity-funding
/api/macro/credit
/api/macro/volatility
/api/macro/cross-asset
```

These reads accept no query parameters. The overview is
`macro_overview_v9`: it returns read time, transport state, latest fact time,
six module availability summaries, and aggregate data quality. It is not a
daily narrative or historical-session product. Each module route returns its
matching persisted schema or `macro_module_unavailable_v1` with a typed reason:

- overview: `macro_overview_v9`
- `macro_rates_fed_v8`
- `macro_economy_inflation_v6`
- `macro_liquidity_funding_v5`
- `macro_credit_v7`
- `macro_volatility_v7`
- `macro_cross_asset_v8`

The five non-rates modules share identity, clocks, status, summary,
contradictions, falsifiers, checkpoints, and evidence lineage. Rates v8
deliberately has no generic `summary`, `top_changes`, contradiction, or
falsifier fields. Its `decision` contract is tenor-native: 2Y/10Y/30Y current
facts, actual baseline dates for 1D/1W/MTD/3M/past-30-day changes,
session-completeness state, 2s10s/10s30s summaries, same-day 10Y/30Y
nominal-real-Breakeven decomposition, window-qualified classifications, and
fact references. It additionally exposes one revisioned official FOMC meeting
calendar and recent typed Treasury auction results. Bill discount rate,
investment rate, and high yield are three independent nullable fields; the
service never collapses them into one first-available value. Bid-to-cover,
offering amount, and indirect/direct/primary-dealer award shares remain
separate facts. Treasury
completed-session curves are decision-primary; FRED
single-tenor series are history/reconciliation only. Treasury cross-sections,
Fed events, credit ladders, and the ETF comparison matrix are explicit typed
fields, not generic chart arrays. Coverage is `complete` or `partial`; Current
Health is `current`, `degraded`, or `unavailable`; rates session completeness
is an independent `complete`, `unaligned`, or `incomplete` axis. History Depth
is `complete`, `partial`, `insufficient`, or `not_required`. Each Dataset
additionally exposes market state and source state. Optional history cannot
lower Current Health. Only declared required windows affect reader-facing
History Depth. One missing or schema-mismatched module produces a typed
unavailable slot without failing the other five. All seven fact payloads use
`macro_module_current` only; the Rates read additionally attaches the
secret-free optional-analysis runtime state from Serve configuration. Reads
never call a provider/model, advance a target, rebuild a projection, or write
fallback content.

Economy v6 adds one required `seasonal_adjustment` enum to every official
release observation. The value is Registry-owned source metadata
(`seasonally_adjusted`, `not_seasonally_adjusted`, or
`seasonally_adjusted_annual_rate`); it is never inferred from the number.
Cross-Asset v8 publishes pair facts for 30, 90, and 252 common daily-return
observations plus a server-owned `correlation_contract`. The browser uses its
default window, minimum common-observation count, supported windows, and
mirrored-matrix presentation rule; it does not invent a correlation default or
persist duplicate reverse/diagonal facts.

Each successful module representation has a weak semantic `ETag` and
`Cache-Control: private, no-cache`; an unchanged `If-None-Match` read returns an
empty `304`. The weak validator safely spans identity and gzip transfer
representations; responses above the transport threshold are gzip-compressed. The
overview remains a read-time snapshot because `read_at_ms` changes per read.

The Dataset and Calculation Registries are code-owned public semantics, not
runtime configuration. Provider config may only enable the free source
families. A dataset's owner, fact family, source/adapter, acquisition clock,
freshness, trust tier, criticality, module membership, and formula identity do
not come from YAML. General cross-asset observations and settlements are Market
facts; macroeconomic series, release events, and official documents are Macro
facts. The legacy generic evidence route, window parameter, bundle/sync
surface, `macro_observations`, and unclassified facts do not exist.

Every Registry row has a stable `concept_id` and `source_role`.
`decision_primary` is authoritative for the current decision; `release`,
`history`, `intraday_proxy`, and `reconciliation_only` remain separately
labelled inputs. Values from different identities are never averaged or
silently substituted. Release facts preserve actual, expected, surprise,
revision, reference date, optional source publication time, and ingestion time
as separate fields.

Treasury owns the current nominal/real curve and FRED owns its history. BLS
owns CPI/labor release facts; BEA public release pages own GDP, PCE, and core
PCE release facts; FRED owns the matching history. The natural-change contract
is Dataset-specific: daily/weekly gaps are bounded and monthly/quarterly
comparisons require the exact calendar lag. A missing period yields a missing
change, never a mislabeled fallback.

The Cross-Asset payload always owns the fixed ETF basket SPY, QQQ, IWM, TLT,
IEF, LQD, HYG, UUP, GLD, and USO plus ES, NQ, RTY, ZB, ZN, GC, CL, and HG
major-futures rows and the Yahoo DXY index. ETF rows use Nasdaq public daily
history for five-year changes, normalization, and correlations, paired with
Yahoo Finance five-minute prices. Futures pair Yahoo five-minute prices with
Yahoo continuous-contract daily history. Both Yahoo lanes and Nasdaq public
history are explicitly `untrusted_proxy`; each row exposes separate history
and price Dataset IDs, its actual market timestamp, price kind, and source
lineage. A closed or maintenance market preserves the last expected bar as
`current`; staleness is measured against the market clock, never wall-clock
age alone. WTI is the separate official FRED/EIA `DCOILWTICO` benchmark. The Rates
payload exposes Treasury nominal and real maturity cross-sections for current,
1W, 1M, and 3M snapshots, matched breakevens, 2s10s/3m10s/5s30s histories,
transparent curve-shape inputs, the official FOMC schedule snapshot, Treasury
auction-demand facts, and the shared SOFR fact. Paid CME probabilities are not
part of the supported contract and no probability proxy is synthesized.

Volatility exclusively owns the official CFE VX settlement curve. A served
`market_settlement_v2` fact requires the official `Expiration Date`; schema
version and expiration participate in both fact hash and settlement identity.
Provable legacy raw rows receive a new append-only v2 revision while the v1 row
remains unchanged. Unprovable v1 rows stay audit-only and are never sorted by a
guessed contract-code expiry. Cross-Asset does not duplicate this curve.

FOMC statement, implementation, minutes, and SEP documents plus Board/Reserve
Bank speeches retain official full body text and source hashes. SEP PDF text is
extracted from the official PDF with bounded page/content limits. The
`macro_document_analysis` native candidate writes one immutable, model/prompt-versioned,
exact-evidence-bound analysis per source body after effective-dated role facts
are available. Institutional FOMC stance and the 90-day officials
communication distribution remain separate. Non-policy material is
`not_policy_signal`; no static official label or universal hawk/dove score
exists. The current immutable-analysis admission window is 550 days for FOMC
materials and 120 days for speeches. Older official bodies remain durable raw
evidence but do not block current module reads.

Document analysis is a supporting capability. Missing, disabled, or
unconfigured analysis cannot lower official Rates/Fed Current Health; the Fed
stance/distribution remains typed `no_call`. The Rates read adds a secret-free
`document_analysis_runtime` state (`disabled`, `unconfigured`, or `active`)
from Serve runtime configuration, while the persisted v7 module remains a
deterministic fact projection. `active`/`worker_active` means only that the
configuration admission conditions are satisfied (`enabled && configured`);
it is not a worker heartbeat or process-liveness claim. Successful immutable
analysis publication and its Dataset/frontier advancement are atomic.

Credit exposes IG/BBB/BB/B/CCC OAS, actual-sample history statistics, IG/HY
effective yields, deterministic comparisons with EFFR and 10Y Treasury, SLOOS
standards and demand for C&I/CRE/consumer, loan delinquency/charge-off facts,
and labelled ETF/CFTC confirmations. Four concurrent credit dimensions are
returned; no composite score exists. Paid TRACE/NAV and unavailable historical
ICE placeholders were deleted from the product contract.

Macro has no second judgment, historical-session, or archive contract. Retired
paths return the ordinary application `404`; there is no alias or fallback
publication.

Migrations `20260801_0235` and `20260801_0236` are irreversible: they remove
retired News acquisition and Macro derived/control history while preserving
current items, material facts, acquisition targets, Fed document analysis, and
the six module rows.
Historical migration `20260801_0237` added an OpenNews recovery boundary;
News migration `20260809_0247` removes it in favor of bounded 12-hour
overlap. `20260801_0238` adds the News push baseline and delivery ledger.
Applying these migrations does not send a message; delivery begins only after an explicit
webhook-backed push configuration and the first enabled reconcile establishes
its baseline. Signing is optional. `20260807_0246` is the irreversible public
World Brief hard cut: it canonicalizes retained OpenNews facts, drops the
retired Story display-title translation table and incompatible Brief state,
removes `normalized_title`/`brief_excluded`, rebuilds Story state, and installs
the singleton selection plus discriminated L1/L2/none publication schema. It
preserves the existing Story Push ledgers, pending/retry deliveries, and
freshness fences. `20260809_0247` installs the pinned public RSS source state,
removes the facet and former Brief run/publication tables, creates the
half-hour `news_brief_current` singleton, clears rebuildable Story/selection
state, and deletes incompatible Push payload rows. The live News schema is
exactly nine tables and has no mixed-schema reader or compatibility view.

### Token images

`/api/token-images/{image_id}` accepts only the persisted lowercase SHA-256 URL identity. Only `ready` assets whose relative path resolves under `~/.tracefold/cache/token-images` are served. Missing rows/files, malformed IDs, absolute paths, and traversal attempts return `404`. Provider URLs are never accepted as a proxy input.

## WebSocket

Clients connect to `/ws`, authenticate, then subscribe:

```json
{"type":"auth","token":"..."}
{"type":"subscribe","cas":[{"ca":"0x...","chain":"eip155:1"}],"symbols":[],"market_targets":[],"replay":100}
```

Authentication accepts exactly `type` and a string `token`. Subscription keys
and value shapes are exact: `symbols` is a string array; `cas` contains
`{ca, chain?}` objects; `market_targets` contains `{target_type, target_id}`
objects; and `replay` is an integer. Retired `handles`, `notifications`,
`ca`, and `tokens` keys, scalar CA values, `address` aliases, extra target
keys, and coercible string/number values are rejected as
`invalid_subscription`. The total filter count and replay count are bounded.
Replay is a PostgreSQL read-side query with batched hydration, not one query
per event or filter. Event replay and event pushes require at least one `cas`
or `symbols` filter; an empty event filter returns and broadcasts no events.
`market_targets` remains an independent subscription for
`live_market_update` pushes. Push message families are `event` and
`live_market_update`. Token Radar never registers `market_targets` and has no
WebSocket patch path; Token Case may subscribe only its active target.

Worker progress is recovered by bounded database catch-up. Provider frames are never emitted as business facts before persistence.

## CLI

`uv run tracefold --help` is the exact CLI source of truth. Stable top-level families are:

- service/config: `serve`, `workers`, `init`, `config`;
- database: `db migrate|health|audit|query-audit`;
- Macro: `macro backfill|backfill-professional|status`;
- read models: `recent`, `search`;
- maintenance: `ops ...` for explicit repair, rebuild, queue inspection/resolution, and diagnostics.

Mutating maintenance commands require an explicit execution flag where the parser offers a dry-run mode. They operate from persisted facts and stable target keys. A rebuild does not create an alternate generation/run identity or make a provider response the source of truth.

`queue-inspect`, `radar-status`, `validate-projections`, and
`audit-token-intent` are strict Serve-role reads.
They do not acquire the maintenance lock, so operators can inspect the running
singleton without interrupting it. Repair and rebuild commands remain
exclusive maintenance operations.

`ops collect-workers-runtime-acceptance --bundle <absolute-path>` is a
read-only production observer with a fixed 1,800-second interval, 181 samples,
10-second cadence, and 15-second maximum gap. It accepts exactly one new
directory outside the checkout and returns non-zero while preserving raw JSONL
if any continuity, identity, capacity, PostgreSQL, resource, or query-plan gate
fails. `ops seal-workers-runtime-acceptance` accepts that repository-owned
collection only after the other typed gates and independent review are bound.

`macro status` reports the bounded acquisition target count/statuses, each of
the six module current rows with its health, history depth, fact cutoff, and
update time, Fed document-analysis job counts, and the secret-free analysis
runtime state (`enabled`, gateway `configured`, configuration-derived
`worker_active`, and model name). It invokes no provider/model and writes
nothing; `worker_active` is admission state, not observed process liveness.

`ops rebuild-market-current --execute` is the bounded, cursor-based repair for
reconstructing `market_tick_current` from persisted `market_ticks`.
News steady state and explicit maintenance use the same complete current
12-hour WorldMonitor calculation from persisted NewsItems. `radar-status`
reports only the current singleton clocks, fingerprints, bounded counts, and
last attempt; it never returns the retired factor payload.

One-shot maintenance commands construct only the dependencies required by the
named domain operation and invoke that bounded operation directly. The
application adapter owns provider/database cleanup and returns exactly
`operation`, `processed`, `failed`, `terminal`, `skipped`, and `preparation`.
`operation` is `resolution_refresh`, `asset_profile_refresh`, or
`token_image_mirror`; counters are non-negative integers and `preparation` is
an object or null. There is no generic result object, free-form notes, or
retired `dead`/`worker_name` field.

Queue resolution is auditable: retry mutates the source queue and resolves terminal evidence in one transaction; quarantine/archive resolves the terminal row without pretending the source work succeeded.

## Contract change discipline

For a public contract change:

1. change the owning domain/application behavior;
2. add a behavior or contract test;
3. update Pydantic/OpenAPI/frontend types when the HTTP shape changes;
4. update this document and the relevant domain architecture map;
5. remove the old name/path instead of adding an alias or dual read/write.

Historical dated audits explain why a hard cut happened; they are not a second runtime specification.
