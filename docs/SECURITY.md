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
  repository and do not commit them. One narrow exception, and only on these
  terms: a *structure-only* derivative may be committed as a test fixture when
  every string outside an explicit structural allowlist — rubric labels, verdict
  enums, symbols, content hashes, opaque identifiers, stable keys — has been
  replaced by a content hash, and a test scans the committed bytes for the shape
  of human language rather than for a list of key names. The allowlist direction
  is the requirement, not a preference: `tests/support/baseline_calibration.py`
  first enumerated the text keys instead and shipped 60 reader-facing Chinese
  cards under a `title_zh` nobody had listed, guarded by an assertion that was a
  tautology for a key-based redactor. Anything richer than that — raw evidence,
  prompts, cards, reviewer prose — stays out of the repository. The database
  copies are content-addressed audit evidence and append-only. Program artifact exports are canonical JSON
  but carry proprietary optimizer-written instructions, so “no credentials”
  does not make them public. Automated
  proposal/optimizer paths may never
  write accepted reviews, holdout membership, reader contracts, release
  thresholds, stable pointers, or canary assignments.

## Single config source boundary

The only Tracefold application configuration file is the operator-owned
`~/.tracefold/config.yaml`. It owns application paths, PostgreSQL role DSNs
and password-file references, the OpenNews token, the RabbitMQ URL, the
Feishu webhook, the API bind address and bearer token, and model
provider/name. The optional `trading.opentrade.token_file` points to the
OpenTrade bearer token used only by App-owned live composition; Trading domain
code never reads the filesystem or receives the token.

The complete secret inventory is: `ws_token` (HTTP API bearer token),
`news.opennews_token`, `llm.api_key`, the optional
`llm.news_reader_card.api_key` (dedicated ReaderCard endpoint), the optional
`llm.news_triage_fallback.api_key` (second Triage endpoint, issue #65),
the optional `llm.news_reader_card_fallback.api_key` (dedicated ReaderCard
fallback endpoint),
`news.broker.url` (carries the broker credentials), `news.push.feishu_webhook_url` and the optional
`news.push.feishu_signing_secret`, the five PostgreSQL password files
(bootstrap, Serve, Review, Workers, migrate), and the optional OpenTrade token
file named by `trading.opentrade.token_file`.
There is no other provider key or credential.

`tracefold init` is the sole default-config generator. It creates
`~/.tracefold/` with mode `0700` and config/bootstrap/Serve/Workers/migrate
secret files with mode `0600`; reruns repair those permissions. Without
`--force`, an existing config is preserved byte-for-byte. `--force` replaces
only the generated config and does not rotate existing PostgreSQL passwords.
Generated defaults contain no live provider, model, or webhook credential and
leave outbound News push disabled. They do not create or populate the optional
OpenTrade token file. A live operator creates it separately as a regular,
non-symlink file of at most 16 KiB with no group/other permission bits
(normally mode `0600`);
diagnostics expose only configured/readable booleans and its resolved path,
never its contents. The provider base URL must be credential-free HTTPS, so
plain HTTP is rejected before the token file is read.

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
`EventSemantics -> deterministic SemanticNormalizer -> ReaderCard.v2 ->
deterministic VerdictAssembler`; callers
cannot supply instructions, demonstrations, topology, routes, retry policy or
artifact paths. A normal judgment uses two serial provider calls. One fast
retry is shared by a route (at most three calls); fallback restarts the full
graph (at most six across the chain). The Program factory owns the route
deadline and call/token budgets. DSPy cache and hidden provider retries are
disabled so the audit trace contains every provider attempt.

EventSemantics uses the primary Triage endpoint. ReaderCard inherits that
endpoint unless the operator supplies the complete `llm.news_reader_card`
triple. The fallback route likewise aliases both Predictor slots only when
`llm.news_reader_card_fallback` is absent; a requested dedicated Reader fallback
must validate or the entire fallback route is disabled. The two Predictors
always receive separate Adapters and code-owned
token caps; endpoint credentials remain application configuration and never
enter the content-addressed Program artifact or secret-free runtime identity.
The identity includes only a one-way endpoint fingerprint beside provider and
model, so different backends cannot share a learning cohort while the URL and
credential remain undisclosed.

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

The only loadable semantic image is one canonical, content-addressed
`news_program_strategy_artifact_v1` JSON document carried in the application
image as `<program_sha256>.json` and selected by its code-owned registry. It
holds a schema version, `factory_id` `tracefold.news.program.factory_v6`, and
the two bounded advisory instructions; `program_sha256` is the canonical hash
of exactly those four values. The loader re-verifies that hash, the schema and
the factory, applies the advisory bounds — NFC, size, forbidden authority and
template markers, secret patterns — and rejects non-canonical or duplicate-keyed
JSON, non-finite numbers, unsafe or secret-bearing keys, a symlinked or
traversing path, and a file whose name is not its own root.
Everything the loader used to re-verify component by component — RulePacks, the
graph, Signatures, the Adapter, the normalizer/assembler contracts, the model
route, the token and deadline budgets — is code, versioned by `factory_id`, so
it is proved by shipping the image rather than by the package hashing itself.
An optimizer's write set is those two instructions and nothing else; there is
no DemoBank to write to, and a Predictor carrying a demo is refused.
Pickle, cloudpickle, DSPy Flex,
dynamic Python/classes, arbitrary callbacks/history, endpoints, credentials and
secret-bearing headers are forbidden artifact state; a database candidate is
not executable merely because it was persisted. Production candidate images
must pass normal code review and be shipped in the registry.
There is no legacy Prompt executor or dynamic compatibility loader to bypass
these checks; Prompt-era database fields are audit-only.

The DSPy GEPA compiler is a cold manual development workflow, not a runtime
Worker. A trusted read-only exporter recomputes the current `program_v7`
development artifact and ordered case/cluster/episode roots. An untrusted
resource-bounded runner receives a read-only input bundle, no DB/holdout/
application credentials, no ambient HOME or arbitrary egress, and provider
access only through a metered proxy sidecar over a fresh named-volume Unix
socket. The runner uses `--network none`; only the sidecar has provider egress
and the short-lived provider secret. Before that secret is mounted, the trusted
host verifies the exact local Docker image ID and independently hashes the
image's News source tree and dependency lock without executing image code. The
expected lock identity is
`tracefold.news.learning.compiler.source_identity.COMPILER_DEPENDENCY_LOCK_SHA256`,
which lives next to the attestation that reads it instead of in the Program
artifact: it crosses a real trust boundary — host to container — and says
nothing about how the Program behaves. A drift test keeps it equal to the
source `uv.lock`, and a wheel that has no `uv.lock` still runs the Program.
Tags and registry manifest references are rejected. The sidecar reserves each
call from the complete positive `llm.news_compiler_tariff`. Task, reflection
and `metric_judge` each have a typed sealed role configuration from which the
secret-free identity, grant, bundle and proxy enforcement are derived.
Reflection alone owns its 32k-token ceiling; the judge has its own endpoint,
instruction/schema/adapter identity, budget, tariff, calls, cost and failure
receipt. The proxy forces each role's output/cache/retry/timeout/temperature/LM
parameters and records canonical per-call usage/cost/finish/error leaves.
Judge failure is explicit unavailable and scores the affected free-text
dimension as failure-as-zero; it never falls back to byte equality, hidden
retry, or a cached failure. Missing actual provider cost or any mismatch fails
before candidate construction. The optimizer can emit only a typed
`ProgramStrategyPatchV1` containing the two advisory instructions. It cannot
modify RulePacks, the graph, Signatures, execution, routes, policy or
stable identity, and it has no demo surface to write to at all. The trusted
side rehashes every receipt payload, applies the
patch to the exact active stable root, and emits an unaccepted candidate.
Timeout, denied access, missing cost, quota breach, invalid patch or extra
output produces no Artifact. Bounded stdout/stderr capture and exact-name
container/network/volume cleanup are part of the signed launch receipt; a
Docker transport failure is never interpreted as proof of cleanup.

Migration `0292` creates the append-only deployment-time `program_v1` learning
epoch; `0293`, `0294` and `0295` append the corrected semantic, expert-quality
and D-generation `program_v2`–`program_v4` epochs. `0298` appends `program_v5`
for factory v3 and candidate-conditioned ToldContext. `0301` preserves all
history and appends `program_v6` for factory/executable v4, policy v10, review
v4 and metric/compiler protocol v3. `0303` appends the current `program_v7` for
factory/executable v5 after the Program/Learning package split. Issue #190
reissues the sole v7 root when canonical identity starts rejecting
NaN/Infinity, and Issue #193 reissues it again as the single-document strategy
artifact under factory v6; in both cases the earlier root is not executable by
the new image. `0304` carries that last cut into the database by tripping every
armed or active canary and writing one migration receipt. It does not re-open
the epoch — identity changed, evidence did not — so accepted `news_review_v4`
truth stays eligible. Every earlier
review, dataset, recording and release receipt is
retained as audit history but is never training, metric-v4,
validation, holdout or promotion evidence for the current Program factory.
The reset is an eligibility hard cut, not permission for an optimizer to
relabel old evidence or delete it.

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
Each Predictor instruction is rendered from `factory_id` plus the artifact's
advisory, so the Program root commits to the exact bytes without a second
per-Predictor digest, and there is no demonstration set. Every request is bound
to the resolved runtime provider/model
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
