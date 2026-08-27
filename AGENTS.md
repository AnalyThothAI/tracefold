# AGENTS.md

Router for coding agents (Codex, Cursor, generic LLM tooling). Substantive
rules live under `docs/`; this file routes to them and does not duplicate them.

The block between the two markers below is generated from
`docs/agents/shared-router.md`, which `CLAUDE.md` also carries. Edit that file
and run `uv run python scripts/sync_agent_router.py --write`; `make check`
fails on drift. Everything after the closing marker is Codex-specific and is
edited here.

<!-- BEGIN SHARED AGENT ROUTER -->

## What this is

`Tracefold Market Research System`: one Python service and CLI named
`tracefold` that turns provider news pushes into audited research signals and
serves them over HTTP / CLI to a React operator console.

Two business capabilities sit over one PostgreSQL store, as siblings rather
than layers: **News V3** (`tracefold.news`) turns OpenNews frames into Events,
judges each one, and delivers at most one reader card; **Trading**
(`tracefold.trading`, #104, disabled by default) is the capital lane that
consumes two public News projections. They never import each other and never
read each other's tables — `tracefold.app` is the only seam that knows both.

`docs/ARCHITECTURE.md` is the single description of how any of that works: the
Kappa/CQRS truth model, the broker-driven Event pipeline, the Gate, the
Program-native Triage and its policy, delivery, the instrument universe, the
Price Review plane, the learning/canary plane, and the Trading state machine.
Read it before changing behavior; do not reconstruct it from this file.

Two facts worth carrying into every task because they are easy to get wrong:

- PostgreSQL material facts and durable ledgers are the only business truth.
  Provider frames, broker messages, process caches, projections, model outputs
  and HTTP responses are not an alternate truth.
- Program identity — the two advisory instructions and `program_sha256`, the
  `factory_id` that versions code-owned prompt/RulePack/route/budget behavior,
  and the policy and metric versions — is release evidence. Changing it is an
  explicit, evidence-gated migration, never a side effect.

## Agent skills

## Completion evidence

Completion evidence is bound to the exact tested HEAD. Results from an earlier
commit, a skipped resource lane, or a modified acceptance contract are not
merge or release evidence. Follow the Verification Evidence Contract in
`docs/DEVELOPMENT.md`.

### Issue tracker

GitHub Issues in `AnalyThothAI/tracefold` are the project request and PRD
tracker. See `docs/agents/issue-tracker.md`.

### Triage labels

Use the canonical label mapping in `docs/agents/triage-labels.md`.

### Domain docs

This is a single-context repository. Follow `docs/agents/domain.md` before
domain exploration; absent optional context or ADR files are not errors.

## Runtime config for real data

Live-data runs use the operator-owned `~/.tracefold/config.yaml` for
application/provider/credential/storage settings. Worker topology and
safety/resource budgets are code-owned. Do not assume repository fixtures,
example YAML, or `.env` files are the active runtime config. Before debugging
provider data or News events against real data, run `uv run tracefold config`
and confirm the reported `config_path` points at `~/.tracefold/config.yaml`.
Never print or copy secret values; report only redacted booleans, paths, and
diagnostic command results.

## Frontend guardrails

Frontend CSS is harness-constrained, not convention-only. Before changing
`web/src` UI code, read `docs/FRONTEND.md`. Global side-effect CSS belongs only
under `web/src/styles`; owner CSS must live beside the component or route that
imports it. Feature CSS must use the owning feature namespace and must not restyle
shared UI internals or Obsidian `.ods-*` selectors. `npm run lint` runs ESLint
plus the frontend architecture harness; do not bypass it after CSS, responsive,
route shell, or shared UI changes.

## Where to read what

| Need                                      | File                                                                                     |
| ----------------------------------------- | ---------------------------------------------------------------------------------------- |
| Install, run, docker                      | `docs/SETUP.md`                                                                          |
| What the system is, and why               | `docs/ARCHITECTURE.md`                                                                   |
| How code here must be written             | `docs/DEVELOPMENT.md` ("Architecture coding rules")                                      |
| Frontend architecture                     | `docs/FRONTEND.md`                                                                       |
| Public surfaces (config, HTTP, CLI)       | `docs/CONTRACTS.md`                                                                      |
| Development, issue specs, design, testing | `docs/DEVELOPMENT.md`                                                                    |
| Secrets, config, authn changes            | `docs/SECURITY.md`                                                                       |
| Operations, workers, PostgreSQL diagnosis | `docs/OPERATIONS.md`                                                                     |
| Business package boundaries               | `docs/ARCHITECTURE.md`; the public Python interfaces are the `tracefold.news` and `tracefold.trading` package roots |
| Durable specs and acceptance              | GitHub Issues; repository conventions are in `docs/agents/issue-tracker.md`              |
| Research notebooks: channels, run, commit  | `notebooks/README.md`                                                                    |
| Auto-generated artefacts                  | `docs/generated/`                                                                        |

CLI surface: `uv run tracefold --help` is the source of truth (snapshot at
`docs/generated/cli-help.md`).

<!-- END SHARED AGENT ROUTER -->

## Codex-only protocol: worktrees, GitHub, planning, and skills

### Worktree by default (do not switch the primary checkout)

The primary checkout (`~/Documents/Code/tracefold`) is the deployment checkout:
it stays on `main`, stays clean, and is the only place `make up` / `make status`
/ `make logs` run. Never switch branches there, edit files there for a task, or
move another task's uncommitted changes. Every code or docs change — including
one-line fixes — starts in its own worktree so tasks and Codex chats can proceed
in parallel.

At the start of every change task, before editing:

1. Run `git worktree list --porcelain`, `git status --short --branch`, and
   `git rev-parse --show-toplevel`.
2. If the chat is already in a Codex-managed, permanent, or sibling worktree
   dedicated to this task, stay there; do not create a nested worktree.
3. If the chat is in the primary checkout, do not edit there. Create an unused
   sibling worktree from `origin/main`, then direct every subsequent command,
   patch, and test at that worktree:

```bash
git fetch origin main
git worktree add -b codex/<issue-or-slug> ../tracefold-<slug> origin/main
cd ../tracefold-<slug> && make sync && (cd web && npm ci) # per-worktree venv/node_modules
```

- In the Codex desktop app, starting the chat with **Worktree** selected is the
  preferred equivalent. A Codex-managed worktree starts at detached `HEAD`; before
  editing, create a `codex/<issue>-<slug>` branch there from `origin/main` (or use
  **Create branch here**). If this chat already has an assigned task branch, keep
  it instead of creating another one.
- Branch from `origin/main`, not local `main`. Name branches
  `codex/<issue>-<slug>` (or `codex/<slug>` when no issue exists). One task, one
  worktree, one branch, one PR.
- An existing worktree belongs to its current task. Never reuse, reset, delete,
  prune, or clean it unless it is the current task's worktree and cleanup is part
  of the authorized finish flow.
- Run tests inside the task worktree (`make test`, plus focused lanes from
  `docs/DEVELOPMENT.md`). The compose stack (`make up`, ports 8765/8766/5672, and
  the `tracefold-*` volumes) is shared and owned by the primary checkout; do not
  run `make up` or `make down` from a worktree.
- Commit and push only from the task branch. Never commit or push on `main`
  directly.
- Finish through PR review and merge. After merge, update and deploy only from
  the primary checkout (`git pull --ff-only origin main`, then `make up`). Remove
  the task worktree and delete its branch only after the merge is confirmed and
  the cleanup is authorized; use `git worktree prune` only for entries verified
  stale.

### GitHub issue, planning, and review

- Non-trivial changes (new table/worker/queue/prompt/policy, hard cuts) start
  with the GitHub issue (`docs/agents/issue-tracker.md`) and a written Codex plan.
  Keep scope and durable decisions in the issue rather than only in chat.
- Use the `code-review` skill on the task branch before opening the PR. Use the
  security-review capability as well when config, auth, credentials, permissions,
  or delivery code changes and that capability is available.
- Live-data debugging follows "Runtime config for real data" above: use read-only
  SQL as the `tracefold_serve` role, run `uv run tracefold config`, and never print
  secrets.
