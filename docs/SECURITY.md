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
and password-file references, provider credentials and URLs, API/auth,
domain/source-family enablement, model provider/name, and logging.

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

`news_triage`, `news_analyst`, and `macro_document_analysis` are the only
production product-model consumers. News Triage is one structured call whose
output never decides delivery by itself: the pure `decide()` rules own the
final decision, model failure is fail-closed, and every verdict row stores
the model intent next to the rule baseline. The News Analyst is a bounded
`deepagents` run with seven read-only tools that execute through a read-only
repository session; it has no filesystem, shell, network, subagent, or write
capability, its evidence citations are verified against the run's own tool
returns, and it can only add a follow-up card after a first card was sent.
Shared News title translation is a bounded presentation adapter: it cannot
write an Item, Event, verdict, or delivery. Item identity, Event identity,
Gate admission, storyline keys, search, and ordering remain deterministic. The
six Macro modules are also deterministic views over persisted facts.

Search and Token Case publish a canonical address for copying and source
navigation; they do not publish a honeypot, holder, liquidity, Smart Money,
contract-risk, or token-safety judgment. Removing those former product gates
does not relax application security: HTTP/WebSocket authentication, exact
request validation, secret handling, PostgreSQL role/transaction integrity,
migration confirmation, and source-fact provenance remain mandatory and are not
product configuration.

`news.opennews_token`, `news.broker.url`, and the configured
`news.opennews_strategy_ids` set are operator-owned secrets/configuration.
Diagnostics expose `opennews_token_configured`, broker `url_configured`,
`opennews_strategy_ids_configured`, and `opennews_strategy_count`; the status
route lists configured and provider-enabled Strategy IDs (non-secret opaque
IDs) so allowlist warnings are actionable, but never the token or broker URL. OpenNews transport exceptions, logs,
generated artifacts, and public source/status responses must never contain the
token, authorization header, or allowlist values.
The current reviewed configuration contains exactly `1018` and `1019`, so
diagnostics expose count `2`; provider-side Listing/Storage Strategies are not
admitted. A future addition requires an explicit reviewed configuration change.

The authenticated WSS automatically sends the account owner's
`strategy.triggered` notifications. Tracefold sends no application subscription
request and performs no Strategy CRUD, account-page scraping, cookie/session
extraction, private webpage API call, or ordinary-news Search replay. On Worker
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
NewsItem metadata after admission and cannot change fact identity, Story
membership, importance, or Brief ordering. Disconnect, overflow, outage, and
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

Title translation uses the ordered operator-owned
`news.translation.deepl_api_keys` first and the direct DeepSeek
`llm.api_key`, `llm.base_url`, `llm.news_triage_model` triple second, only for
Events that will be delivered. DeepL keys are secrets: diagnostics expose only
whether any key is configured and the key count. Only the exact Event leader
title is sent to either provider; provider metadata, URLs, verdicts, the
webhook, and the signing secret are never included. Presentation rows are
non-secret and bounded to original/display title, outcome, provider, and a
sanitized fallback code.

News Triage receives the Event title/content excerpt (wrapped as untrusted
material), Gate facts, the storyline status bar, and the watchlist symbols;
News Analyst additionally receives Triage field conclusions (never the free
text rationale) and reads bounded tool outputs wrapped as external content.
Neither receives credentials, webhook material, or unrelated corpus context;
prompts are byte-frozen constants and raw model responses are never persisted
beyond the validated verdict payload and bounded trace.

RabbitMQ credentials live only in `news.broker.url`; the compose service binds
AMQP and management ports to `127.0.0.1` by default and uses a
`TRACEFOLD_RABBITMQ_PASSWORD` environment override. Consumers connect with one
robust connection; queue names are prefixed by `news.broker.name_prefix`.

Fed document analysis receives one bounded official source body plus
effective-dated role and prior-signal context. It has no provider or web tool,
and every returned excerpt is verified against that frozen body before
immutable insertion. Regulation, technology, inclusion, and ceremonial
material may remain `not_policy_signal`/`no_call`; the worker cannot create a
permanent official label or universal score.

The Macro overview and six module reads expose only persisted current module
state. They contain no credentials, invoke no provider/model, and cannot
advance targets or rebuild state. Direct provider data, live web, and News are
not alternate Macro fact sources. Public APIs return only validated product
payloads and bounded sanitized errors, never raw credentials, authorization
headers, hidden reasoning, or unsanitized provider failures.

## Sensitive change confirmation

Ask before changing authentication, authorisation, billing, or data-deletion behaviour.

## Frontend WebSocket token

The `ws_token` reaches the browser through the same config schema. Do not embed it in committed source; the frontend reads it from the page bootstrap injected by `api/`.
