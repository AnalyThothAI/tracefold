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

`news_world_brief` and `macro_document_analysis` are the only production
product-model consumers.
Shared News title translation is a bounded presentation adapter, not a product
model: it cannot write a NewsItem, Story, score, or semantic read model.
News acquisition, NewsItem classification, Story identity, importance
scoring, membership, search, and ordering remain deterministic. Its exact-title
row preserves the original and adds only display metadata; Push references that
same decision rather than owning or duplicating translation. The six Macro
modules are also deterministic views over persisted facts.

Token Radar publishes a canonical address for copying and source navigation;
it does not publish a honeypot, holder, liquidity, Smart Money, contract-risk,
or token-safety judgment. Removing those former product gates does not
relax application security: HTTP/WebSocket authentication, exact request
validation, secret handling, PostgreSQL role/transaction integrity, migration
confirmation, and source-fact provenance remain mandatory and are not Radar
configuration.

When `news.rss_enabled` is explicitly true, News RSS acquisition accepts only
the code-owned HTTPS catalog. The Adapter
does not use automatic redirects: it follows at most two hops and, before every
request, rejects credentials in the URL, non-HTTPS targets, local/reserved host
forms, failed or empty DNS results, and any resolved address that is not
globally routable. Feed failures preserve the previous current facts until the
normal 96-hour expiry; malformed or unsafe-redirect responses cannot publish
an empty replacement snapshot.

`news.opennews_token` and the exact configured
`news.opennews_strategy_ids` set are operator-owned secrets/configuration.
Diagnostics expose only `opennews_token_configured`,
`opennews_strategy_ids_configured`, and `opennews_strategy_count`; source/status
responses never list the configured set. OpenNews transport exceptions, logs,
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
redirects. Persisted Item source snapshots and shared title-presentation rows never contain the
webhook, signing secret, timestamp, signature, or complete rendered card. There
is exactly one Feishu attempt after the durable `sending` fence and no retry.

Shared title presentation uses the ordered operator-owned
`news.title_presentation.deepl_api_keys` first and the direct DeepSeek
`llm.api_key`, `llm.base_url`, and `llm.news_brief_model` triple second. DeepL
keys are secrets: diagnostics expose only whether any key is configured and the
key count, never values or the process-local active index. A permanent DeepL
authentication or quota rejection advances that active index for future Items;
it never exposes the rejected key or tries another key for the current Item.
Only the exact immutable Item title is sent to either provider; asset
annotations, score, URL, description, Story data, Feishu webhook, and signing
secret are never included. Provider URLs and keys never enter logs, public
status, generated artifacts, or presentation rows. Persisted presentation
metadata is non-secret and bounded to original/display title,
translated/not-needed/fallback outcome, policy/provider, a sanitized fallback
code, and elapsed milliseconds. Feed/detail and every final card keep the
original title visible when a distinct Chinese display title is used. Current
Push rows reference the exact shared title decision and do not duplicate its
presentation payload; the renamed legacy Push JSON remains audit-only.

The public News Brief L1 model receives only ordered primary headlines,
primary reporting origins, and distinct-source counts. It receives no Article
description/body, provider AI metadata, unrelated corpus context, user profile,
preference, personalized filter, Push/Feishu material, or source credential.
L2 receives only the one eligible primary headline. Both have no provider
fetch, filesystem, shell, or arbitrary database capability. Every accepted L1
response passes the same citation/proper-noun/number/date gates used before the
sealed payload replaces the Brief current singleton; raw responses and prompts
are never persisted.

News Brief uses the code-owned local Ollama endpoint first, the same configured
direct DeepSeek endpoint/key/model triple second, and optional Groq last. The
direct triple must be entirely present or entirely absent; no implicit URL or
model is supplied. Diagnostics expose only configured booleans and bounded
provider/model labels, failure codes, and clocks; they never probe or expose an
endpoint, key, Authorization header, Retry-After contents, prompt, or response.

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
