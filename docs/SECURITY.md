# Security

> **Scope.** Owns secret handling, supported config-source rules, and the change-confirmation requirement for sensitive subsystems. Operational invariants live in `OPERATIONS.md`.

## Secrets

- Never print or log secrets, tokens, cookies, or `.env` values.
- Never commit `.env`, credentials, private keys, or generated config files.
- When validating live data, use `uv run tracefold config` for
  redacted config-path and configured-status diagnostics. Do not paste or copy
  provider keys from `~/.tracefold/config.yaml` into chat, docs, tests,
  shell history, or source files.
- Frozen ReviewDesk datasets, Program artifacts, candidate manifests, model recordings, shadow
  observations, evaluation reports, and deployment receipts carry no
  credentials, but may carry provider news content, prompts and reader-facing
  copy. Treat exported copies as business data: keep them outside the
  repository and do not commit them. The database copies are content-addressed
  audit evidence and append-only. Program artifact exports are canonical JSON
  but can contain proprietary instructions, demonstrations and reviewed News
  examples, so “no credentials” does not make them public. Automated
  proposal/optimizer paths may never
  write accepted reviews, holdout membership, reader contracts, release
  thresholds, stable pointers, or canary assignments.

## Single config source boundary

The only Tracefold application configuration file is the operator-owned
`~/.tracefold/config.yaml`. It owns application paths, PostgreSQL role DSNs
and password-file references, the OpenNews token, the RabbitMQ URL, the
Feishu webhook, the API bind address and bearer token, and model
provider/name.

The complete secret inventory is: `ws_token` (HTTP API bearer token),
`news.opennews_token`, `llm.api_key`, the optional
`llm.news_triage_fallback.api_key` (second Triage endpoint, issue #65),
`news.broker.url` (carries the broker credentials), `news.push.feishu_webhook_url` and the optional
`news.push.feishu_signing_secret`, and the five PostgreSQL password files
(bootstrap, Serve, Review, Workers, migrate).
There is no other provider key or credential.

`tracefold init` is the sole default-config generator. It creates
`~/.tracefold/` with mode `0700` and config/bootstrap/Serve/Workers/migrate
secret files with mode `0600`; reruns repair those permissions. Without
`--force`, an existing config is preserved byte-for-byte. `--force` replaces
only the generated config and does not rotate existing PostgreSQL passwords.
Generated defaults contain no live provider, model, or webhook credential and
leave outbound News push disabled.

Worker topology, clocks, deadlines, batches, leases, retries, timeouts,
resource budgets, history limits, product windows/venues, and model
reservations are code-owned.

Do not introduce a second application config path, shadow config in
environment variables, or move code-owned safety budgets into
`config.yaml`. Schemas and public config contracts live in `CONTRACTS.md`.

## Model capability boundary

`news_triage` is the only production product-model consumer. Its sole
Interface is `SemanticJudge.judge(TriageContext) -> SemanticJudgment`. The
production Adapter executes the fixed DSPy graph
`EventSemantics -> ReaderCard -> deterministic VerdictAssembler`; callers
cannot supply instructions, demonstrations, topology, routes, retry policy or
artifact paths. A normal judgment uses two serial provider calls. One fast
retry is shared by a route (at most three calls); fallback restarts the full
graph (at most six across the chain). The artifact owns the route deadline and
call/token budgets. DSPy cache and hidden provider retries are disabled so the
audit trace contains every provider attempt.

The Program output never decides delivery by itself: pure `decide()` rules own
the final decision, Program failure is fail-closed, and every verdict row stores
model intent next to the rule baseline. The Predictors have no tools, agent
loop, retrieval, filesystem, shell, subagent or write capability; their only
outbound capability is the Adapter's configured model endpoint. One Event
persists one final judgment and one card — the two internal calls do not restore the
Analyst stage removed in #57. The card's Chinese text is the Triage verdict's
`headline_zh` and `why_zh`; no separate title, translation, or follow-up
provider exists. Item identity, Event identity, Gate admission, storyline keys,
`decide()` and feed ordering remain deterministic.

The only loadable semantic image is a canonical, content-addressed, state-only
`ProgramArtifact` JSON manifest/state pair carried in the application image and
selected by its code-owned registry. The loader verifies schema, Program/state
hashes, fixed factory/topology/signatures, source and dependency-lock identity,
Adapter/assembler/input contracts, exact files and safe path shape before use.
The dependency-lock digest is a package-owned generated identity checked
against `uv.lock` in development, so wheel loading never trusts or searches an
ambient repository. Parsed demonstration JSON is recursively scanned and must
match the exact model-visible input schema; audit `event_id`/fact ids, endpoints
and secret keys cannot be smuggled through a JSON string. It fails closed on
unknown or mismatched state. Pickle, cloudpickle, DSPy Flex,
dynamic Python/classes, arbitrary callbacks/history, endpoints, credentials and
secret-bearing headers are forbidden artifact state; a database candidate is
not executable merely because it was persisted. Production candidate images
must pass normal code review and be shipped in the registry.
There is no legacy Prompt executor or dynamic compatibility loader to bypass
these checks; Prompt-era database fields are audit-only.

The DSPy GEPA compiler is a cold manual development command, not a runtime
Worker. It receives accepted `program_v2` development episodes only and must be
given explicit metric-call, total task/reflection-model-call,
provider-cost-in-microusd limits and a seed. It has no authority to read
validation/holdout, write accepted truth, register, deploy or promote. Its
output remains an unaccepted candidate until the ordinary release chain and
code review carry it into an image.

Migration `0292` creates the append-only deployment-time `program_v1` learning
epoch; `0293` preserves it and appends `program_v2` for the corrected semantic
retry state machine and hardened restatement sentinel. Prompt-era and
`program_v1` reviews, datasets, recordings and release receipts are retained as
audit history but are never training, validation, holdout or
promotion evidence for this Program factory. The reset is an eligibility hard
cut, not permission for an optimizer to relabel old evidence or delete it.

PostgreSQL runtime roles are code-owned:
`src/tracefold/platform/postgres/alembic/runtime_roles.sql`, executed by the
`20260818_0275` baseline migration and extended by the #112 migrations,
creates the non-login `tracefold_owner` plus `tracefold_serve`
(`default_transaction_read_only=on`), `tracefold_workers` (pipeline/control
writes), and `tracefold_migrate`. Serve has SELECT plus INSERT only on
`news_reviews` and `news_external_miss_snapshots`; it has no UPDATE/DELETE on
those append-only facts and no write grant on Event, verdict, delivery,
learning-artifact or control tables. Every ordinary Serve transaction remains
read-only. Only the two bearer-authenticated ReviewDesk POST routes explicitly
open one transaction as read-write through the existing Serve pool. Learning
freeze/evaluate and canary control run under Workers, while assignment and
runtime/deployment receipts are append-only.

HTTP authentication is one bearer token: `/api/bootstrap` hands `ws_token`
to the served console and every other `/api/*` route requires it as
`Authorization: Bearer <ws_token>`; `/healthz`, `/readyz`, and `/metrics` are
unauthenticated liveness/telemetry surfaces (the compose stack publishes the
HTTP port on loopback). There is no WebSocket endpoint and no second
authentication scheme. Exact
request validation, secret handling, PostgreSQL role/transaction integrity,
migration confirmation, and source-fact provenance remain mandatory and are
not product configuration.

`news.opennews_token` and `news.broker.url` are operator-owned secrets.
Diagnostics expose `opennews_token_configured` and broker `url_configured`,
never the token or the URL. OpenNews transport exceptions, logs, generated
artifacts, and public source/status responses must never contain the token or
the authorization header.

Which Strategies feed News is account configuration held in the OpenNews
dashboard, not in Tracefold (#126). No Strategy ID and no Strategy count reaches
the browser; Workers read the list only where recovery genuinely needs it,
because the provider's hits endpoint is per-strategy.

The authenticated WSS automatically sends the account owner's
`strategy.triggered` notifications. Tracefold sends no application subscription
request and performs no Strategy CRUD, account-page scraping, cookie/session
extraction, private webpage API call, or provider news-search replay. On Worker
startup and WSS reconnect, recovery may call only the official bounded
`/open/strategy_list` and `/open/strategy_hits` interfaces: it reads the
account's enabled list fresh because the hits endpoint is per-Strategy, then
repairs audited coverage intervals. It does not verify or maintain a local
allowlist. Strategy
definitions and provider-side enablement remain account authority. A successful
handshake proves authentication/connectivity only; it does not prove Strategy
existence, enablement, delivery completeness, or lossless history.

Accepted event metadata is bounded to provider score/signal/grade/assets/source
and the deterministic matching Strategy ID/name/source-type/observed-engine-type
provenance union. The full Strategy definition and metrics payload are never
copied into public data. Provider ratings and provenance are descriptive
NewsItem metadata after admission and cannot change fact identity, Event
membership, priority, or feed ordering. Disconnect, overflow, outage, and
provider non-delivery create sanitized typed incidents with bounded cause,
close-code, interval, and recovery state. Reconnect restores current WSS health
but never marks an interval recovered without official Strategy-hit evidence;
the ledger stores no token, raw exception, payload, or provider reason text.

`news.push.feishu_webhook_url` and the optional
`news.push.feishu_signing_secret` are operator-owned secrets. They are reported
only as configured booleans and never appear in logs, errors, status payloads,
generated artifacts, or persisted delivery rows. A webhook disclosed outside
the operator config should be treated as compromised and rotated before live
use. Enabling delivery requires the supported Feishu HTTPS webhook. When a
signing secret is configured, the Adapter sends a timestamp and signature;
without one it deliberately sends an unsigned body containing neither field.
Unsigned delivery has weaker request authentication and is an explicit
operator choice, not a fallback after a signing error. In both modes the
Adapter accepts only the configured Feishu webhook boundary and never follows
redirects. Persisted delivery rows store the rendered card (code facts plus sanitized AI
copy) for audit but never the webhook, signing secret, timestamp, or signature.
There is exactly one Feishu attempt after the durable `sending` row and no
retry; a crash between send and ack terminalizes as `ambiguous_after_crash`.

News Triage receives the Event title/content excerpt (wrapped as untrusted
material), Gate facts, the storyline status bar, and the watchlist symbols. It
never receives credentials, webhook material, or unrelated corpus context.
Each Predictor instruction and demonstration set is hash-bound in the Program
artifact and every request is bound to the resolved runtime provider/model
identity. The trace persists only validated semantic/card output plus bounded
finish/usage/cost metadata; raw provider responses and hidden reasoning are not
persisted. Exact record/replay refuses an unrecorded request or runtime-model
identity mismatch and never falls through to live I/O.

RabbitMQ credentials live only in `news.broker.url`; the compose service binds
AMQP and management ports to `127.0.0.1` by default and uses a
`TRACEFOLD_RABBITMQ_PASSWORD` environment override. Consumers connect with one
robust connection; queue names are prefixed by `news.broker.name_prefix`.

Public APIs return only validated product payloads and bounded sanitized
errors, never raw credentials, authorization headers, hidden reasoning, or
unsanitized provider failures.

## Sensitive change confirmation

Ask before changing authentication, authorisation, billing, or data-deletion behaviour.

## Frontend API token

The `ws_token` reaches the browser through `/api/bootstrap`. Do not embed it in committed source; the frontend reads it from that bootstrap response and sends it as the bearer token on every other API call.
