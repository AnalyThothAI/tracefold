# Security

> **Scope.** Owns secret handling, supported config-source rules, and the change-confirmation requirement for sensitive subsystems. Operational invariants live in `RELIABILITY.md`.

## Secrets

- Never print or log secrets, tokens, cookies, or `.env` values.
- Never commit `.env`, credentials, private keys, or generated config files.
- When validating live data, use `uv run tracefold config` for
  redacted config-path and configured-status diagnostics. Do not paste or copy
  provider keys from `~/.tracefold/config.yaml` into chat, docs, tests,
  shell history, or source files.

## Single config source boundaries

The supported operator-owned config files are
`~/.tracefold/config.yaml` and
`~/.tracefold/workers.yaml`. `config.yaml` owns application,
provider, credential, storage, API, and public-surface settings.
`workers.yaml` owns worker runtime knobs such as enabled state,
intervals, batches, concurrency, leases, attempts, explicit boundary
timeouts, and retry bounds.

Do not introduce a third config path, shadow config in environment
variables, or duplicate worker runtime knobs under `config.yaml`.
Schemas and public config contracts live in `CONTRACTS.md`.

`~/.tracefold/rsshub.env` is an optional infrastructure credential injection
file consumed only by the pinned RSSHub Compose sidecar. It is not a third
Tracefold application configuration source: application code must not read,
log, validate, copy, or report it. Never commit that file or its cookie/token
values.

## Macro Thesis Agent capability boundary

`macro_thesis` and the separately published `news_world_brief` are the only
production product-model consumers. News acquisition, NewsItem classification,
Story identity, scoring, and serving never call a model.
Macro Thesis uses DeepAgents only as one checkpointed structured-output graph.
An exact-model harness profile removes todo, filesystem, `execute`, and `task`
from the model-visible tool surface and disables the general-purpose subagent.
The graph has no research tool loop. One compact, cutoff-bound decision view
is embedded in the invocation; raw historical arrays and transport receipts
remain in PostgreSQL rather than model context. Allowed module and exact-fact
references are enumerated in that frozen input.
The research graph and Reviewer graph are separate invocations. The Reviewer is
not a research subagent and receives the frozen Evidence Pack plus the exact
draft hash; `revise` permits at most one corrected draft.

Fed document analysis is the only event-granular model lane in live Macro. It
receives one bounded official source body plus effective-dated role/prior-signal
context, has no provider or web tool, and must return exact excerpts that are
verified against that body before immutable insertion. Regulation, technology,
inclusion, and ceremonial material may remain `not_policy_signal`/`no_call`;
the worker cannot create permanent official labels or a universal score.

Every Thesis evidence tool is bound to one frozen session scope. It may read
only the immutable Evidence Pack selected by `evidence_pack_id` and prior
immutable Macro publications. The pack itself was compiled from cutoff-bounded
persisted Market/Macro facts. Macro Thesis has no live or hidden News
dependency. There is no Macro Thesis workspace or calculation shell; graph
checkpoints contain only execution state. Direct provider, live web, arbitrary
SQL, and the News Story Interface are not alternative Macro fact sources.
Evidence selection, causal analysis, counterevidence, the one mainline,
optional alternative, tensions, module roles, and condition-bound asset
outlook remain Agent-owned; the independent Reviewer may return `pass`,
`revise`, or `block`.

The overview, six module pages, and `/macro/research` are read-only views over
persisted module and one immutable Thesis history. Coverage, Current Health,
History Depth, and transport freshness are transparent decision metadata, not
permission middleware or a process health gate. They expose no credentials and
make no provider/model call.

The graph uses the frozen scope ID as its durable PostgreSQL checkpoint
`thread_id`. Checkpoints may contain model messages, todo state, and virtual
filesystem scratch state required to resume a run. Per-scope execute workspace
files live under the operator app home. The public API exposes only validated
Thesis, Reviewer disposition, deterministic Live Delta/Outcome Replay, and a
bounded sanitized run status; it never exposes checkpoint payloads,
credentials, hidden reasoning, raw provider secrets, or unsanitized model
failures. Unsupported coding-agent model identities fail before invocation as
terminal configuration errors.

## Sensitive change confirmation

Ask before changing authentication, authorisation, billing, or data-deletion behaviour.

## Frontend WebSocket token

The `ws_token` reaches the browser through the same config schema. Do not embed it in committed source; the frontend reads it from the page bootstrap injected by `api/`.
