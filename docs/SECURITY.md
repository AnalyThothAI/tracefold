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
  is the requirement, not a preference. Anything richer than that — raw evidence,
  prompts, cards, reviewer prose — stays out of the repository. The database
  copies are content-addressed audit evidence and append-only. Program artifact exports are canonical JSON
  but carry proprietary optimizer-written instructions, so “no credentials”
  does not make them public. Automated
  proposal/optimizer paths may never
  write accepted reviews, holdout membership, reader contracts, release
  thresholds, stable pointers, or canary assignments.

## Single config source boundary

The only Tracefold application configuration file is the operator-owned
`~/.tracefold/config.yaml`. It owns application paths, the PostgreSQL DSN and
password-file reference, the OpenNews token, the RabbitMQ URL, the
Feishu webhook or Telegram target/token-file reference, the API bind address and bearer token, and model
provider/name. `trading.control` contains only its enable switch, Telegram
token/webhook-secret file references, chat/user allowlists, and notification
target. `trading.execution.credentials` contains the closed file
references for the one Binance USD-M Runtime profile. The Signal-only Workers
process has no execution-secret mount or secret-reading path. Serve has neither.

The complete secret inventory is: `ws_token` (HTTP API bearer token),
`news.opennews_token`, `llm.api_key`, the optional
`llm.news_reader_card.api_key` (dedicated ReaderCard endpoint), the optional
`llm.news_triage_fallback.api_key` (second Triage endpoint, issue #65),
the optional `llm.news_reader_card_fallback.api_key` (dedicated ReaderCard
fallback endpoint),
`news.broker.url` (carries the broker credentials), `news.push.feishu_webhook_url` and the optional
`news.push.feishu_signing_secret`, the Telegram bot-token file named by
`news.push.telegram_bot_token_file`, the two PostgreSQL password files
(bootstrap and the shared `tracefold` application login), and the Binance files
named by `trading.execution.credentials.api_key_file` / `api_secret_file`.
When Trading control is enabled, its bot-token file and webhook-secret file are
also secrets; the generated canonical names are `telegram_bot_token` and
`telegram_webhook_secret`. A shared token filename does not merge News and
Trading semantics: each adapter keeps its own target and message contract.
There is no Hyperliquid execution credential and no other provider key or
credential.

`tracefold init` is the sole default-config generator. It creates
`~/.tracefold/` with mode `0700` and config/bootstrap/application database
secret files with mode `0600`; reruns repair those permissions. Without
`--force`, an existing config is preserved byte-for-byte. `--force` replaces
only the generated config and does not rotate existing PostgreSQL passwords.
Generated defaults contain no live provider, model, webhook, or bot credential
and leave outbound News push disabled. They create empty mode-`0600`
`telegram_bot_token`, `telegram_webhook_secret`, `binance_usdm_api_key`, and `binance_usdm_api_secret`
placeholders; an empty file is `unconfigured`, never a credential. They do not
create or populate an execution key. A live
operator populates each required provider file as a regular,
non-symlink file of at most 16 KiB with no group/other permission bits
(normally mode `0600`);
diagnostics expose only configured/readable booleans and resolved paths, never
contents.

Compose mounts only the generated Telegram token and webhook-secret filenames,
and only into Workers. Serve receives neither file nor its contents. If outbound push is
explicitly enabled with an absent, empty, malformed, symlinked, or
over-permissive token file, Workers fails startup with a stable sanitized reason
instead of running without the requested delivery boundary.

The Trading webhook is absent unless `trading.control.enabled=true`. When
present it remains on the loopback Workers listener; an operator-owned HTTPS
reverse proxy may expose only `/telegram/control`. Every request must carry the
Telegram secret-token header, match both chat and user allowlists, fit the
bounded body, and parse under the closed command grammar. Secrets and raw
payloads are never persisted or echoed. A 2xx reply proves only durable intent
recording. Telegram delivery failures stay outside the execution callback and
cannot block protection, exit, or reconciliation.

The Nautilus service is excluded from the default Compose model and remains
absent while execution is disabled. Canonical paper/live deployment enables
the explicit `execution` profile and requires Nautilus readiness (`alive &&
execution_safe`, independent of whether new entries are armed). It alone has
read-only mounts for the Binance pair; absent, empty, symlinked, oversized, or
over-permissive files fail startup. Neither plaintext nor path enters
PostgreSQL, HTTP, logs, artifacts, or Issues. No execution credential is
exposed to Workers, Serve, News, RabbitMQ, or public HTTP.
The complete private-account proof uses Nautilus 1.231 private Binance adapter
members only through `nautilus_1231_binance_compat.py`. Keeping that dependency
in one pinned seam prevents an upgrade fallback from bypassing the complete
positions + regular orders + Algo orders proof.

Worker topology, clocks, deadlines, batches, leases, retries, timeouts,
resource budgets, history limits, product windows/venues, and model
reservations are code-owned.

Do not introduce a second application config path, shadow config in
environment variables, or move code-owned safety budgets into
`config.yaml`. Schemas and public config contracts live in `CONTRACTS.md`.

## Model capability boundary

The production model consumers are the News semantic Program and the optional
post-delivery progression verifier. The Program's sole Interface is
`SemanticJudge.judge(TriageContext) -> SemanticJudgment`. The
production Adapter executes the fixed two-Predictor native DSPy Program
`EventSemantics -> deterministic SemanticNormalizer -> ReaderCard.v2 ->
deterministic VerdictAssembler`; callers
cannot supply instructions, topology, routes, retry policy or artifact paths. A
normal judgment uses exactly two serial provider calls. DSPy's JSONAdapter may
make one format fallback per Predictor, so one route is capped at four calls;
fallback restarts the complete Program and the judgment is capped at eight. The
Program factory owns the route deadline and call/token budgets. DSPy's public
Signature/Predict/JSONAdapter surface owns model-visible rendering and
structured-output parsing, while stock `dspy.LM`/LiteLLM owns provider I/O.
Provider retry and cache are disabled. Tracefold's typed
`AuditedConfiguredLM(dspy.BaseLM)` wrapper does not construct HTTP, messages or
`response_format`; it records the safe request identity, usage and exactly one
terminal disposition for every physical delegate call.

EventSemantics uses the primary Triage endpoint. ReaderCard inherits that
endpoint unless the operator supplies the complete `llm.news_reader_card`
triple. The fallback route likewise aliases both Predictor slots only when
`llm.news_reader_card_fallback` is absent; a requested dedicated Reader fallback
must validate or the entire fallback route is disabled. The two Predictors
always receive separate explicit LM bindings and code-owned
token caps; endpoint credentials remain application configuration and never
enter the content-addressed Program artifact or secret-free runtime identity.
The identity includes only a one-way endpoint fingerprint beside provider and
model, so different backends cannot share a learning cohort while the URL and
credential remain undisclosed.

The Program output never decides delivery by itself: pure `decide()` rules own
the final decision, Program failure is fail-closed, and every verdict row stores
model intent next to the rule baseline. The Predictors have no tools, agent
loop, retrieval, filesystem, shell, subagent or write capability; their only
outbound capability is the configured DSPy LM endpoint. One Event
persists one final judgment and one card — the two internal calls do not restore the
Analyst stage removed in #57. The card's Chinese text is the Triage verdict's
`headline_zh` and `why_zh`; no separate title, translation, or follow-up
provider exists. Item identity, Event identity, Gate admission, storyline keys,
`decide()` and feed ordering remain deterministic.

The only loadable semantic image is one content-addressed
`news_program_strategy_artifact_v1` JSON document carried in the application
image as `<program_sha256>.json` and selected by its code-owned registry. It
holds a schema version and the two complete Predictor instructions;
`program_sha256` is the canonical hash of exactly those three values, and the
loader re-verifies it. That check is not tamper-proofing — it is the property the
cohort model rests on, since a file whose bytes disagree with its identity would
make "which Program produced this evidence" unanswerable.

Issue #319 removed the rest of what used to sit here, on the operator's ruling
that this system's threat model is a single operator on a local network with an
offline optimizer and no adversarial party. Gone: `..`/symlink/`resolve` path
armouring, byte-exact canonical enforcement and round-trip re-checking on read,
duplicate-key rejection, the filename-equals-sha check, recursive scanning for
credential-shaped artifact state, and the injection-marker blacklist on
instructions. Each defended against someone who could already write to the code
being executed, and each cost something real — the canonical check refused a
pretty-printed file, the blacklist refused ordinary editorial prose containing a
URL.

What the loader still applies to an instruction is NFC, a byte and
estimated-token budget, and non-empty. None of the three is a defence: NFC
because two encodings hash differently, the budget because every call pays for
those bytes, non-empty because a Predictor without a prompt is not one.

One secret-handling mechanism survives #319, by explicit operator decision, and
it is worth naming because its reason is not the obvious one.
`program.lm._scrub_detail` substitutes credential-shaped text out of a
provider's error body before that body is carried on a failed attempt's trace.
What it catches is not an attacker: it is a provider handing our own key back —
`Invalid API key: sk-…` is ordinary provider behaviour — into
`news_verdicts.trace`, a table that is backed up, pasted into issues, and
retained for a year. That is an accident, not an attack, so removing the
adversary from the threat model does not dispose of it. The same function's
200-byte cap is a resource control rather than a security one: a provider may
answer with a whole HTML page, once per failed attempt, into a JSONB column.

Everything else the Program runs on — the graph, Signatures, the Adapter, the
normalizer and assembler, the model route, the token and deadline budgets — is
code, proved by shipping the image. Its identity travels beside the artifact as
the computed `envelope_sha256`; `docs/ARCHITECTURE.md` describes the model.
The taxonomy optimizer may replace only the EventSemantics instruction; it
copies ReaderCard byte-identically into the complete two-instruction candidate,
so no demo or endpoint path exists to reach a provider. Production candidate
images pass normal code review and are shipped in the registry; a database
candidate is not executable merely because it was persisted, and Prompt-era
database fields are audit-only.

The GEPA optimizer is a cold manual development workflow, not a runtime
Worker. Issue #202 deleted the container platform that used to surround it: the
sealed image, the launcher, the metered proxy sidecar, the seccomp policy, the
tariff, the three-party `CompilerBuildAttestation` and the runner. That platform
answered one question — *where were these two strings produced* — and its threat
model was "the optimizer might return code". It cannot: public `dspy.GEPA`
optimizes the one named EventSemantics `dspy.Predict`, and `run_gepa` refuses a
winner that is not exactly that one Predict with empty demos before it restores
the unchanged ReaderCard instruction. Proving provenance was never what made a
candidate safe to ship.

What actually bounds the job is what it holds, and that is now a short list.
`news learning run` reads one frozen development corpus through the shared application login, closes the
database before provider work, and then holds two model endpoints plus a typed in-process budget. It has no
database writer, broker, delivery, canary or promotion credential. Zero-call readiness runs before endpoint
construction and requires both `objective.compilable` and `development_profile.ready`.

The task and reflection roles are separate `ModelExecutionIdentity` values. Their endpoint URLs are
fingerprinted rather than stored, credentials never enter hashes or traces, and bounded provider errors are
scrubbed. The meter reserves the declared per-call price before a request, settles provider-reported cost
after it, and records each role's input/output/cached/total tokens, calls, cost, failures and wall-clock time
in the same report. Reflection alone has the code-owned 32k-token ceiling. The taxonomy optimizer has no
semantic judge, no ReaderCard call, no tool or code-generation authority, and no private DSPy API.

Every terminal state writes `news_optimization_run_report_v3`; only `ADVANCE` also writes
`news_prompt_candidate_v2`. The only mutable field is the EventSemantics instruction. ReaderCard is copied
byte-identically, and the report publishes before/after hashes, bytes, estimated tokens, growth and diff
beside the public native GEPA parent/score/subscore state. Registration independently re-applies the patch,
re-projects the dataset and re-derives Objective Plan v3. Generator provenance grants no release authority;
future holdout, blind pairwise, shadow, canary and manual promotion remain mandatory.

Optimization usage v3 distinguishes a proven zero metric count from an unavailable count: preflight
refusals record `0`, while an interrupted GEPA compile with no public result records `null`. Physical model
calls, tokens and cost remain exact; the report never infers a metric count from them or private GEPA state.

If dynamic code generation or an agent graph ever becomes a candidate again, the
sandbox threat model has to be rebuilt with it, under a new Issue. It is not
kept warm for a hypothetical (#202 §6.3).

Migration `0292` creates the append-only deployment-time `program_v1` learning
epoch; `0293`, `0294` and `0295` append the corrected semantic, expert-quality
and D-generation `program_v2`–`program_v4` epochs. `0298` appends `program_v5`
for factory v3 and candidate-conditioned ToldContext. `0301` preserves all
history and appends `program_v6` for factory/executable v4, policy v10, review
v4 and metric/compiler protocol v3. `0303` appends the then-current `program_v7` for
factory/executable v5 after the Program/Learning package split. Issue #190
reissues the sole v7 root when canonical identity starts rejecting
NaN/Infinity, and Issue #193 reissues it again as the single-document strategy
artifact under factory v6; in both cases the earlier root is not executable by
the new image. `0304` carries that last cut into the database by tripping every
armed or active canary and writing one migration receipt. `0305` carries #193's
compile-record cut the same way: it admits the `compile_record` artifact kind,
keeps `compile_receipt` in the constraint so retired rows stay readable, and
trips open activations again, because a candidate registered against the old
chain names a receipt that no longer validates. Neither re-opens the epoch —
identity changed, evidence did not — so accepted `news_review_v4`
truth stays eligible. Every earlier
review, dataset, recording and release receipt is
retained as audit history but is never training, metric-v8,
validation, holdout or promotion evidence for the current Program factory.
`0315` then records #288's exact source route and factory-v7 cut without
rewriting or appending the `program_v7` epoch row. Accepted review labels remain
immutable truth, but prior-factory judgments are audit-only under the exact
current-bundle filter and the factory-v7 eligible cohort starts at zero.
The reset is an eligibility hard cut, not permission for an optimizer to
relabel old evidence or delete it.

PostgreSQL has one ordinary non-superuser application login, `tracefold`, plus
the `tracefold_app` bootstrap superuser whose login is disabled after empty
PGDATA initialization. Alembic, Serve, Workers, Nautilus, CLI, and database
diagnostics share `storage.postgres.dsn` and
`postgres_database_password`; the bootstrap password is not mounted into those
containers. This is an explicit single-host LAN/single-trust-domain decision.
Adding another role requires a distinct trust-domain owner, not merely another
process name.

Database access still has mechanical boundaries. Stable `application_name`
values attribute sessions. The internet-facing Serve HTTP pool sets
connection-level read-only mode and exposes no HTTP write route. The review CLI
uses a separate short-lived connection and explicit transaction. PK/FK/UNIQUE,
business CHECKs, conditional updates, append-only/state-machine triggers,
maintenance locks, and repository transaction ownership remain authoritative;
internal role permission denial is not a business invariant. Runtime code does
not execute DDL, and Compose orders the one-shot migration before steady
processes. `pg_stat_statements` is naturally visible for the complete
application identity without a statistics-reader workaround.

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
allowlist. History payload failures expose only a closed reason code; raw
response bodies and provider values never enter errors, logs, status, metrics,
or artifacts. Strategy
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

`news.push.feishu_webhook_url`, the optional
`news.push.feishu_signing_secret`, and the Telegram bot-token file are
operator-owned secrets. They are reported
only as configured booleans and never appear in logs, errors, status payloads,
generated artifacts, or persisted delivery rows. Telegram diagnostics expose
only whether a secure token file and channel target are configured, never the
token, file contents, or numeric channel ID. A webhook or bot token disclosed
outside the operator config should be treated as compromised and rotated before
live use. Enabling delivery requires exactly one complete provider. When a
signing secret is configured, the Adapter sends a timestamp and signature;
without one it deliberately sends an unsigned body containing neither field.
Unsigned delivery has weaker request authentication and is an explicit
operator choice, not a fallback after a signing error. In both modes the
Adapter accepts only the configured Feishu webhook boundary and never follows
redirects. The Telegram Adapter sends only to the fixed private-channel ID
loaded from operator configuration, verifies the returned message belongs to
that same channel, uses a fixed HTTPS Bot API origin, follows no redirects, and
stores only a token-keyed, domain-separated HMAC-SHA-256 target digest, never
the channel ID, in the delivery receipt. The keyed digest is not enumerable
from the small private-channel-ID space and changes when the bot token rotates.
The credential never enters the httpx request URL seen by its INFO logger: a
fixed-origin transport injects `/bot{token}/` only while building the TLS wire
path and converts transport failures to sanitized codes.
Before the first send it verifies exact target ID, private
channel type, bot identity, administrator status, and post permission; a public
channel, group, supergroup, or mismatched target fails closed. Preflight and
`sendMessage` are separate finite operations: preflight completes before the
durable `sending` row exists, and the operation behind that row contains only
`sendMessage`. After that message is verified and durably settled `sent`, the only permitted mutations are
`editMessageText` and receipt-bound `deleteMessage` for the exact same configured channel and positive message ID
from the canonical receipt. The
Adapter rejects a receipt with a different provider, target digest, invalid message ID, or missing original send
timestamp. It independently verifies the edit response still names the configured channel and same message ID.
The typed receipt has an exact allowlist (`provider`, `message_id`, `pushed_at_ms`, `target_sha256`, and optional
`edited_at_ms` / `deleted_at_ms`); extra provider text, URLs, or metadata fail validation. Storage binds
`pushed_at_ms` as well as
message and target identity before accepting either edit intent or settlement.
The Bot API transport allowlist contains only the fixed preflight methods, `sendMessage`,
`editMessageText`, and `deleteMessage`; arbitrary bot methods and destinations remain impossible. Each operation uses a seven-second application budget; every HTTP
phase is capped at 1.25 seconds and later calls stop when the monotonic budget is
exhausted. Socket timeouts are inactivity limits rather than a strict wall-clock
guarantee, so DNS or a continuously slow peer can outlive that budget. A timed-out
preflight thread still cannot progress into a later send.
Trade links are a Telegram-only presentation capability, not stored card content. The delivery stage creates a
typed target only when the displayed ticker and an exact Binance, Hyperliquid, OKX, Lighter, or Bitget catalogue
contract agree. The Telegram Adapter independently reconstructs an allowlisted credential-free venue URL and
wraps only the exact ticker token in HTML after escaping all other card text. A malformed, aliased, unsupported,
or inconsistent target therefore degrades to plain text and cannot introduce an arbitrary link; Feishu ignores the
ephemeral target and receives the persisted card unchanged.
The source hyperlink is reconstructed only from the stable card's existing original-source action and remains
HTTPS-only with no redirects followed by Tracefold. Provider text never supplies HTML: the Adapter escapes the
normalized source label and every other card character before inserting the validated URL into one anchor. It
recognizes a publisher brand from a hostname only on the exact domain or a dot-delimited subdomain, so a name
such as `jin10.com.evil.test` cannot inherit the trusted reader label. It does not create an inline keyboard or
a second destination. Ephemeral typed market/timing presentation values contain no credential. A successful edit
may persist the final rendered card plus only provider lifecycle timestamps in the canonical receipt; it never
persists price-provider requests, arbitrary URLs, channel IDs, or credentials.
Persisted delivery rows store the
rendered card (code facts plus sanitized AI copy) for audit but never provider
credentials or signatures. There is exactly one initial provider-send attempt
after the durable `sending` row and no retry; a crash between send and ack
terminalizes as `ambiguous_after_crash`. An enrichment or edit failure cannot retract, retry, or terminalize that
initial send. The desired replacement is durable before `editMessageText`; a provider/settlement uncertainty is
recorded as edit ambiguity. Startup must reconcile inherited intents before consuming, and a bounded runtime sweep
retries stale reconciliation after transient database failures. Error logs contain only a sanitized exception class
or bounded adapter code.

News Triage receives the Event title/content excerpt (wrapped as untrusted
material), Gate facts, the storyline status bar, and the watchlist symbols. It
never receives credentials, webhook material, or unrelated corpus context.
Each Predictor is sent the artifact's instruction unchanged, so the Program root
commits to the exact bytes without a second per-Predictor digest, and there is no
demonstration set. Every request is bound
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
