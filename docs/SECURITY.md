# Security

> **Scope.** Owns secret handling, supported config-source rules, and the change-confirmation requirement for sensitive subsystems. Operational invariants live in `OPERATIONS.md`.

## Secrets

- Never print or log secrets, tokens, cookies, or `.env` values.
- Never commit `.env`, credentials, private keys, or generated config files.
- When validating live data, use `uv run tracefold config` for
  redacted config-path and configured-status diagnostics. Do not paste or copy
  provider keys from `~/.tracefold/config.yaml` into chat, docs, tests,
  shell history, or source files.

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
`news.push.feishu_signing_secret`, and the four PostgreSQL password files.
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

`news_triage` is the only production product-model consumer. It is one
structured call with a
byte-frozen system prompt whose output never decides delivery by itself: the
pure `decide()` rules own the final decision, model failure is fail-closed,
and every verdict row stores the model intent next to the rule baseline. It
has no tools, agent loop, filesystem, shell, network, subagent, or write
capability, and one Event gets exactly one judgment and one card — the second
model stage (the Analyst lane) was removed in #57. The card's Chinese text is
the Triage verdict's `headline_zh` and `why_zh`; no separate title,
translation, or follow-up provider exists. Item identity, Event identity, Gate
admission, storyline keys, and feed ordering remain deterministic.

PostgreSQL runtime roles are code-owned:
`src/tracefold/platform/postgres/alembic/runtime_roles.sql`, executed by the
`20260818_0275` baseline migration, creates the non-login `tracefold_owner`
plus `tracefold_serve` (`default_transaction_read_only=on`, SELECT only),
`tracefold_workers` (SELECT/INSERT/UPDATE/DELETE), and `tracefold_migrate`
when run by the bootstrap superuser, verifies that role contract, and applies
the grants. Serve never writes; `tracefold news control` and `tracefold news label` write
`news_control_state` and `news_event_labels` from the CLI through the Workers
role, and no HTTP route mutates News state.

HTTP authentication is one bearer token: `/api/bootstrap` hands `ws_token`
to the served console and every other `/api/*` route requires it as
`Authorization: Bearer <ws_token>`; `/healthz`, `/readyz`, and `/metrics` are
unauthenticated liveness/telemetry surfaces (the compose stack publishes the
HTTP port on loopback). There is no WebSocket endpoint and no second
authentication scheme. Exact
request validation, secret handling, PostgreSQL role/transaction integrity,
migration confirmation, and source-fact provenance remain mandatory and are
not product configuration.

`news.opennews_token`, `news.broker.url`, and the configured
`news.opennews_strategy_ids` set are operator-owned secrets/configuration.
Diagnostics expose `opennews_token_configured`, broker `url_configured`,
`opennews_strategy_ids_configured`, and `opennews_strategy_count`; the status
route lists configured and provider-enabled Strategy IDs (non-secret opaque
IDs) so allowlist warnings are actionable, but never the token or broker URL. OpenNews transport exceptions, logs,
generated artifacts, and public source/status responses must never contain the
token, authorization header, or allowlist values.
The current reviewed configuration contains exactly `1018`, `1352`, and
`1353`, so diagnostics expose count `3`; `1019` is disabled provider-side and
not configured. A future change requires an explicit reviewed configuration
change.

The authenticated WSS automatically sends the account owner's
`strategy.triggered` notifications. Tracefold sends no application subscription
request and performs no Strategy CRUD, account-page scraping, cookie/session
extraction, private webpage API call, or provider news-search replay. On Worker
startup and WSS reconnect, the same token may call only the official bounded
`/open/strategy_list` and `/open/strategy_hits` interfaces to verify the exact
configured allowlist and repair audited coverage intervals. Strategy
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
never receives credentials, webhook material, or unrelated corpus context; the
system prompt is the byte-frozen constant `TRIAGE_SYSTEM_PROMPT` (English
instructions, Chinese reader text) whose SHA-256 is recorded in each verdict
trace, and raw model responses are never persisted beyond the validated
verdict payload and bounded trace.

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
