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

`~/.tracefold/rsshub.env` is an optional infrastructure credential injection
file consumed only by the pinned RSSHub Compose sidecar. It is not a third
Tracefold application configuration source: application code must not read,
log, validate, copy, or report it. Never commit that file or its cookie/token
values.

## Macro Thesis Agent capability boundary

`macro_thesis` and the separately published `news_world_brief` are the only
production product-model consumers. News acquisition, NewsItem classification,
Story identity, scoring, and serving never call a model.
Macro Thesis uses DeepAgents as one Thin structured-output composition. Each
durable attempt invokes the graph once and the provider model exactly once.
The explicit capability boundary makes filesystem, todo, task, execute,
search, summarization, business tools, subagents, and checkpoint writes
unreachable. There is no research tool loop, Reviewer invocation, or revision
loop. The model sees only one deterministic, cutoff-frozen, bounded
`MacroResearchInputV1` with allowed exact evidence and condition identities.

Fed document analysis is the only event-granular model lane in live Macro. It
receives one bounded official source body plus effective-dated role/prior-signal
context, has no provider or web tool, and must return exact excerpts that are
verified against that body before immutable insertion. Regulation, technology,
inclusion, and ceremonial material may remain `not_policy_signal`/`no_call`;
the worker cannot create permanent official labels or a universal score.

The immutable Evidence Pack is compiled from cutoff-bounded persisted
Market/Macro facts. Macro Thesis has no live or hidden News dependency,
workspace, calculation shell, arbitrary SQL, provider source, or web source.
Direct provider data, live web, and the News Story Interface are not
alternative Macro fact sources.
Evidence selection, causal analysis, counterevidence, the one mainline,
optional alternative, tensions, module roles, and condition-bound asset
outlook remain Agent-owned. Code owns source identity, deterministic facts,
condition predicates, citation closure, stable IDs, and the four publication
gates.

The overview, six module pages, and `/macro/research` are read-only views over
persisted module and one immutable Thesis history. Coverage, Current Health,
History Depth, and transport freshness are transparent decision metadata, not
permission middleware or a process health gate. They expose no credentials and
make no provider/model call.

The public API exposes only validated Thesis v2, deterministic Live
Delta/Outcome Replay, Recovery, and bounded sanitized run status. It never
exposes raw provider mappings, credentials, hidden reasoning, raw provider
secrets, or unsanitized failures. Historical v1 Reviewer rows remain audit-only
and cannot enter current state or authorize publication. Unsupported model
identities fail before invocation as terminal configuration errors.

## Sensitive change confirmation

Ask before changing authentication, authorisation, billing, or data-deletion behaviour.

## Frontend WebSocket token

The `ws_token` reaches the browser through the same config schema. Do not embed it in committed source; the frontend reads it from the page bootstrap injected by `api/`.
