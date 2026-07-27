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

## Macro Research Agent capability boundary

`macro_research` and the separately published `news_world_brief` are the only
production product-model consumers. News acquisition, NewsItem classification,
Story identity, scoring, and serving never call a model.
DeepAgents keeps its native todo planning, checkpoint-backed virtual
filesystem, real `execute`, context management, structured final output, and
dynamic `task` delegation.
The parent may delegate to the declared evidence analyst, cross-asset
challenger, and skeptical editor as its research requires; Tracefold does not
force a fixed tool or review sequence.

Every evidence tool is bound to one frozen completed-session scope. It may read
only the immutable Evidence Pack selected by `evidence_pack_id` and prior
immutable Macro publications. The pack itself was compiled from cutoff-bounded
persisted Market/Macro facts. Macro Research has no live or hidden News
dependency. A native composite backend provides `execute` and a shared
`/workspace/` for calculation while keeping ordinary files and large results
in checkpoint state. Direct provider, live web, arbitrary SQL, and the News
Story Interface are not alternative Macro fact sources. Planning, evidence
sufficiency, gaps, professional judgment, section structure, counterevidence,
and Chinese expression remain Agent-owned; the explicit reviewer may return
`pass`, `revise`, or `block`.

The overview and six decision-module pages are read-only views over persisted
module, judgment, and research state. Dataset quality and module readiness are
transparent decision metadata, not permission middleware or a process health
gate. They expose no credentials and make no provider/model call.

The graph uses the frozen scope ID as its durable PostgreSQL checkpoint
`thread_id`. Checkpoints may contain model messages, todo state, and virtual
filesystem scratch state required to resume a run. Per-scope execute workspace
files live under the operator app home. The public API exposes only the
published artifact and a bounded sanitized audit; it never exposes checkpoint
payloads, credentials, hidden reasoning, raw provider secrets, or unsanitized
model failures.

## Sensitive change confirmation

Ask before changing authentication, authorisation, billing, or data-deletion behaviour.

## Frontend WebSocket token

The `ws_token` reaches the browser through the same config schema. Do not embed it in committed source; the frontend reads it from the page bootstrap injected by `api/`.
