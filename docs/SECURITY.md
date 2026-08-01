# Security

> **Scope.** Owns secret handling, supported config-source rules, and the change-confirmation requirement for sensitive subsystems. Operational invariants live in `RELIABILITY.md`.

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

Worker topology, clocks, deadlines, batches, leases, retries, timeouts,
resource budgets, history limits, product windows/venues, and model
reservations are code-owned.

Do not introduce a second application config path, shadow config in
environment variables, or move code-owned safety budgets into
`config.yaml`. Schemas and public config contracts live in `CONTRACTS.md`.

## Model capability boundary

`news_world_brief`, `news_story_push_translation`, and
`macro_document_analysis` are the only production product-model consumers.
News acquisition, NewsItem classification, Story identity, importance
scoring, and serving are deterministic and never call a model. Push
translation receives only one frozen selected headline and cannot modify a
NewsItem or Story. The six Macro modules are also deterministic views over
persisted facts.

`news.opennews_token` is an operator-owned secret. Configuration diagnostics
expose only a configured boolean. OpenNews transport exceptions, current
metadata, logs, generated artifacts, and public source/status responses must
never contain the token or authorization header. Provider AI ratings are
descriptive NewsItem metadata and cannot change identity, Story membership, or
importance.

`news.push.feishu_webhook_url` and `news.push.feishu_signing_secret` are
operator-owned secrets. They must be configured together, are reported only
as configured booleans, and never appear in logs, errors, status payloads,
generated artifacts, or persisted delivery rows. A webhook disclosed outside
the operator config is treated as compromised and rotated before live use.
Signing is mandatory when push is enabled. The Adapter accepts only the
configured Feishu HTTPS webhook boundary and never follows redirects.

The push translator reuses the existing `llm.api_key` and `llm.base_url`; it
does not introduce another credential path. It calls code-owned
`deepseek-v4-flash` with thinking explicitly disabled and a bounded title-only
prompt/output. Translation failure cannot expose provider text or credentials
through an error and cannot block the frozen English fallback delivery.

The News Brief model receives only the bounded selected Story evidence. It has
no source credential, provider fetch, filesystem, shell, or arbitrary database
capability. A validated immutable publication is inserted only after its
citation indexes close against the selected Stories.

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
