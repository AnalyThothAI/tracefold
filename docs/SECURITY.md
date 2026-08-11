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
Optional News Push title translation is an outbound presentation adapter, not
a product model: it cannot write a NewsItem, Story, score, or read model.
News acquisition, NewsItem classification, Story identity, importance
scoring, membership, and ordering remain deterministic. Push preserves the selected
OpenNews original headline and freezes any translation only inside its delivery
envelope. The six Macro modules are also deterministic views
over persisted facts.

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

`news.opennews_token` is an operator-owned secret. Configuration diagnostics
expose only a configured boolean. OpenNews transport exceptions, current
metadata, logs, generated artifacts, and public source/status responses must
never contain the token or authorization header. Provider AI ratings are
descriptive NewsItem metadata and cannot change identity, Story membership, or
importance.

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
redirects. The frozen delivery payload records only the non-secret `auth_mode`
(`signed` or `unsigned`), never the webhook, signing secret, timestamp, or
signature. A retry whose frozen mode differs from current configuration is
terminal before network submission.

News Push title translation reuses the operator-owned direct DeepSeek
`llm.api_key`, `llm.base_url`, and `llm.news_brief_model`; there is no
`news.push.translation` secret, second copy, inferred endpoint/model, or
fallback provider. When translation is available, only the selected title is
sent to DeepSeek; coins, score, URL, description, Story data, Feishu webhook, and
signing secret are never included. Configuration diagnostics expose only
`translation_enabled`, derived from Push enablement plus the direct triple, and
`translation_configured`, derived from that triple's availability. The
provider URL and key never enter logs, public status,
generated artifacts, or frozen payloads. Frozen presentation metadata is
non-secret and bounded: headline mode, target language, adapter kind, engine,
prompt version, sanitized fallback code, and—only for dispatched translation
work—the attempt clock and elapsed milliseconds.

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
