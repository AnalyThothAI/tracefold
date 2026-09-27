# Public contracts

[Handbook](README.md) · [Architecture](ARCHITECTURE.md) · [Generated references](generated/README.md)

This page owns public boundary semantics and the route inventory. Exact fields,
required properties, parameter limits and response schemas are generated from the
actual HTTP application, not copied into a second two-thousand-line manual.

## 1. Contract authorities

| Surface | Source of truth | Change verification |
| --- | --- | --- |
| HTTP | [Routes](../tracefold/app/http/routes/), [schemas](../tracefold/app/http/schemas/), [OpenAPI](generated/openapi.json) | OpenAPI drift and exact public-surface tests |
| CLI | [Parsers](../tracefold/app/cli/parsers/), [commands](../tracefold/app/cli/commands/), [generated help](generated/cli-help.md) | CLI help regeneration and command tests |
| Configuration | [Typed models](../tracefold/platform/config/models.py), [loader](../tracefold/platform/config/loader.py), generated `tracefold init` defaults | Settings/initialization tests; redacted config inspection |
| Storage | Owner repositories and [Alembic](../tracefold/platform/postgres/alembic/versions/); [schema reference](generated/db-schema.md) | Migration/integration checks and database introspection |
| Execution handoff | [execution_contracts.py](../tracefold/trading/execution_contracts.py) | Strict contract, scope, stream and Runtime tests |
| Frontend aliases | [Generated types](../web/src/lib/types/openapi.ts), [frontend contracts](../web/src/lib/types/frontend-contracts.ts) | Type generation, typecheck and frontend tests |

There are no forwarding aliases for removed internal routes, settings or business
owners. Historical values in an immutable archive do not make them valid current
inputs. Historical schema operations belong to [Migrations](MIGRATIONS.md).

## 2. HTTP is a read-only surface

**Every current `/api/*` operation is GET.** Serve's public database pool is
read-only. The console cannot submit orders, change runtime control, accept News
reviews or promote a candidate. Authenticated operator writes use their explicit
CLI/control boundary, not a hidden browser mutation route.

The browser obtains the configured bearer through `/api/bootstrap`; the historical
setting name `ws_token` is retained, but does not imply a WebSocket client or grant
order authority. Status reads expose measured persisted runtime/capability state.
No model, venue query or research replay is run merely because a browser requests
a projection. See [Security](SECURITY.md) for access boundaries.

### Live API inventory

| Method and route | Mounted operation |
| --- | --- |
| `GET /api/bootstrap` | Bootstrap |
| `GET /api/news/events/{event_id}` | Get News Event |
| `GET /api/news/feed` | Get News Feed |
| `GET /api/news/market` | Get News Market |
| `GET /api/news/market/{item_id}` | Get News Market Item |
| `GET /api/news/quotes` | Get News Quotes |
| `GET /api/news/status` | Get News Status |
| `GET /api/news/symbols/{base}` | Get News Symbol |
| `GET /api/news/wallets` | Get News Wallets |
| `GET /api/news/wallets/events` | Get News Wallet Events |
| `GET /api/news/wallets/events/{episode_id}` | Get News Wallet Event |
| `GET /api/status` | Status |
| `GET /api/trading/cases` | Get Trading Cases |
| `GET /api/trading/cases/{case_id}/replay` | Get Trading Case Replay |
| `GET /api/trading/executions` | Get Trading Executions |
| `GET /api/trading/status` | Get Trading Status |

Parameter and response details are in [OpenAPI](generated/openapi.json). The
[public-contract tests](../tests/contract/test_openapi_drift.py) compare this
inventory to the mounted API and require every live route to be named.

### Removed routes

<!-- retired-routes:begin -->
- `/api/news/wallets/cards` (#641): replaced by token episode list/detail; no redirect.
- the GMGN lane: `/ws`, `/api/recent`, `/api/events/by-ids`, `/api/search`,
  `/api/search/inspect`, `/api/token-case`, `/api/target-posts`,
  `/api/target-social-timeline`, `/api/live-market`, `/api/token-images/*`,
  `/api/token-radar`, `/api/stocks-radar`;
- the Trading reads: `/api/trading/signals`,
  `/api/trading/execution/observations` and `/api/trading/execution/state`
  (#537 PR-5), and `/api/trading/gate` and `/api/trading/gate/{event_id}`
  (#589 PR-2);
- the manual console command route: `POST /api/trading/execution/commands`
  (#624); both GET and POST return `404` with no compatibility handler;
- the console mounts `/app` and `/app/*` (#589 PR-5). They answer the same
  `404`: the SPA has no `app` route, so serving `index.html` there returned the
  console's own "404 Not Found" screen under a `200`.
<!-- retired-routes:end -->

These names are historical exclusions, not compatibility endpoints. Use current
CLI reads for internal transport inspection rather than reintroducing a browser
surface that has no product consumer.

## 3. News contracts

[News](modules/news.md) describes Item revision → frozen input → adopted
EventUpdate → independent notification plan → selected intent → actual receipt.
[OI](modules/oi.md) retains the separate typed market path. IDs for an Item, Event,
content revision, semantic work and notification intent are not interchangeable.

Current editorial documents use `news_event_update_v2`; immutable v1 history
retains its original hash. Claims contain stable refs, mode, phase, content kind,
typed assets, supported time precision and exact evidence citations. Changes,
source relationships, implications and open questions have separate contracts in
[updates/contracts.py](../tracefold/news/updates/contracts.py). Omission is not an
implicit retraction or question resolution.

[News topics and source authority](NEWS_TAXONOMY.md) owns the retained IPTC topic
codebook and source-authority classifier. The former four-axis taxonomy and
Program `fact_kind` output are not the current API contract. Publisher authority
is not independent verification of a quoted allegation. Unknown market identity
stays unknown rather than inheriting a coarse Event-level asset class.

Event detail separates **input progress**, **adopted content**, and **reader
outcome**. No-change input can advance done without another adopted head, and a
failed latest revision preserves the last valid head. Historical verdicts appear
as `legacy_verdict`, not reconstructed EventUpdate claims. A completed plan with a
failed card is not a sent notification.

Editorial send results are `sent`, `not_sent` or `ambiguous`, with exact body,
digest and provider identity. Proved not-sent retries retain the intent; ambiguous
side effects are not blindly retried. Market notification vocabulary remains its
own `pending`, `sending`, `sent`, `failed`, `unknown`, `unavailable` lifecycle.
The two products are not forced into one misleading delivery enum.

Current quote movement and fixed-horizon Event reactions also answer different
questions. Wallet first/current snapshots and target-time/actual-time price samples
retain their meanings. Missing baselines or coverage are not fabricated zero returns.

### Retained legacy review vocabulary

`FACT_KINDS` remains the closed vocabulary for reading legacy verdicts and their
ReviewDesk rubric. Its code-owned order is:

`state_change|new_quantity|level_crossed|period_record|quantified_flow|official_measure|statement|recap|schedule|promotion`

This preserves historical read/review compatibility; it is **not** the new Agent's
EventUpdate claim-level `content_kind` schema and does not reactivate old policy.

## 4. Trading contracts

The editorial handoff is **`news_public_update_v1`**, with a stable public update
identity, adopted revision, changed claims/citations, explicit predecessor/affected
refs, first availability, semantic completion and deterministic source text.
It is generated from adopted knowledge, not a ReaderCard. Historical
`headline`/`why`-only payloads are rejected on this path.

| Public kind | Consumer behavior |
| --- | --- |
| `catalyst_delta` | Eligible changed claims can select a target and create a Trigger/Case. Restatement and unresolved `possible_new` are not manufactured catalysts. |
| `source_update` | Idempotent Trading amendment before target selection; no new Trigger/Case, TTL extension, order cancellation or extra trading authority. |

Corrections/replacements follow explicit claim refs across Events and respect the
knowledge-time cutoff. A corrected source can invalidate a still-unsubmitted entry
as `source_corrected`. OI preserves its separate source-key scope. News and Trading
are connected through public contracts, not shared internal SQL access.

[Trading](modules/trading.md) owns Trigger, frozen Case, read-only Agent proposal,
pure compiler and `TRADE`, `NO_TRADE`, `WATCH`. Case state, analysis status, action
and publication are separate axes. WATCH creates a bounded conditional analysis,
not a recursive Agent loop or an immediate venue order.

Executable transport uses `TradeSignalV3`, binding account slot, entry scope,
target mapping, Case/decision and root-bounded expiry. `OperatorIntentV1` records
explicit authenticated control; execution observations establish actual results,
not inferred fills. Exact fields remain in
[execution_contracts.py](../tracefold/trading/execution_contracts.py).

Replay APIs read frozen records rather than rerun models. Research price paths,
historical simulation and native execution returns are different denominators.
Unknown model cost, stale account state or incomplete native fills remain unknown.
See [Execution](modules/execution.md) for venue authority and recovery.

## 5. Configuration and operator lifecycle

The only application config is `~/.tracefold/config.yaml`; there is no `.env` or
static example fallback. Top-level and nested typed models reject unsupported keys.
Complete endpoint credentials and explicit connection identity are validated
rather than inferred. Some Analysis budgets are operator settings; fixed internal
limits remain with their owning modules.

Initialization preserves existing config/password bytes and repairs permissions;
`init --force` replaces only config defaults. [Setup](SETUP.md) owns file locations,
credential-dependent capabilities and the safe startup sequence. It intentionally
does not copy a history of deleted config keys into fresh-install instructions.

`make up` applies policy/migrations and manages Serve, Workers and Analysis.
`make runtime-*` manages the separate Nautilus image/process. `make down` stops the
execution owner first and preserves volumes. There is one configured Binance
connection, no in-process Paper/live execution selector or simulated fill writer.
Actual environment choice and account controls remain explicit configuration.

## 6. CLI read/write and evidence discipline

[Generated help](generated/cli-help.md) owns command names, options and required
arguments. A command's write authority follows its handler: ordinary reads,
assisted draft files, accepted review writes, scoped repair, maintenance,
and execution commands are not interchangeable actions.

Model-backed calibration/analysis operations require their explicit budgets and record
actual calls. Historical replay must not refresh root validity, overwrite accepted
history or publish retrospective Signals. An operator control receipt is not a
venue fill. [Operations](OPERATIONS.md) owns the supported command procedures and
[Security](SECURITY.md) the authentication/credential boundary.

## 7. Changing a contract

Change the owning typed contract, its callers and executable tests together.
Regenerate affected outputs using [the listed generators](generated/README.md),
then inspect the diff. Remove obsolete internal consumers instead of adding a
parallel compatibility model. Update the corresponding module guide when semantics
change; a generated schema diff alone does not explain an ownership change.

External public/provider compatibility decisions need explicit handling, but
historical Issue prose is not an additional runtime contract. The
[execution-owner history](adr/0002-trading-execution-owner-hard-cuts.md) exists to
interpret old archives, not to keep retired execution paths alive.
