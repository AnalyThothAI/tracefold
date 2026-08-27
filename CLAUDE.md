# CLAUDE.md

Claude-specific router. Substantive rules live under `docs/`; this file routes
to them and adds the Claude-only Skills / Plan-mode / Worktree protocol below.

The block between the two markers below is generated from
`docs/agents/shared-router.md`, which `AGENTS.md` also carries. Edit that file
and run `uv run python scripts/sync_agent_router.py --write`; `make check`
fails on drift. Everything after the closing marker is Claude-specific and is
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

## Claude-only protocol: worktrees, plan mode, skills

### Worktree by default (do not switch the primary checkout)

The primary checkout (`~/Documents/Code/tracefold`) is the deployment checkout:
it stays on `main`, stays clean, and is the only place `make up` / `make status`
/ `make logs` run. Never `git checkout <branch>` there, never edit files there
for a task, and never leave it dirty. Every code or docs change — including
one-line fixes — starts in its own worktree so several tasks (and several
sessions) can proceed at the same time:

```bash
git fetch origin main
git worktree add -b claude/<issue-or-slug> .claude/worktrees/<slug> origin/main
cd .claude/worktrees/<slug> && make sync && (cd web && npm ci) # per-worktree venv/node_modules
```

- `EnterWorktree` (Claude Code tool) is the same thing: it creates
  `.claude/worktrees/<name>` from `origin/main` and moves the session there.
  Sibling directories (`../tracefold-<slug>`, the historical `codex/*`
  worktrees) are also fine; `.worktrees/` and `.claude/worktrees/` are
  gitignored.
- Branch from `origin/main`, not from local `main`; name branches
  `claude/<issue>-<slug>` (or the agent's own prefix). One task, one worktree,
  one branch, one PR.
- Before editing, run `git worktree list` and `git status` — an existing
  worktree belongs to its current task; do not reuse or clean it up.
- Tests run inside the worktree (`make test`, focused lanes from
  `docs/DEVELOPMENT.md`). The compose stack (`make up`, ports 8765/8766/5672,
  the `tracefold-*` volumes) is shared and owned by the primary checkout: do not
  run `make up`/`make down` from a worktree.
- Finish: PR → merge → in the primary checkout `git pull --ff-only origin main`
  → `make up` (deployment) → `git worktree remove .claude/worktrees/<slug>` and
  `git branch -d claude/<slug>`; `git worktree prune` for stale entries.
- Commit only on the task branch; never commit or push on `main` directly.

### Plan mode and skills

- Non-trivial changes (new table/worker/queue/prompt/policy, hard cuts) start
  with the GitHub issue (`docs/agents/issue-tracker.md`) and a plan; use plan
  mode to agree scope before editing.
- Run `/code-review` on the branch before opening the PR; `/security-review`
  when config, auth, credentials, or delivery code changes.
- Live-data debugging follows "Runtime config for real data" above: read-only
  SQL as the `tracefold_serve` role, `uv run tracefold config`, never print
  secrets.
