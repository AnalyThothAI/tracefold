# `notebooks/` — the Tracefold research workspace

Every research Jupyter notebook in this repository lives here. This file is the
executable version of the rule: how a notebook gets its data, how it is run, and
what gets committed.

A notebook here answers a question about data that already exists. It is not a
worker, not a scheduled job, and never a second source of truth: PostgreSQL
material facts and durable ledger artifacts remain the only business truth, and
nothing a notebook computes changes that. Notebooks read; they never write a
business table, accept a Review, register a dataset, promote a candidate, or
place an order.

Published research prose stays in `docs/research/` — essays, and the `.sql` /
`.json` evidence they cite. What moved here is the *executable* part.

## Layout

```text
notebooks/
  README.md            this file
  news-*.ipynb         one notebook per question, domain prefix, flat
  trading-*.ipynb
  snapshots/           the .sql + .json pairs that channel-C notebooks read
  research/            multi-file research programs: modules, an entry script, its self-tests
```

Notebooks are flat and top level. The date belongs in the filename
(`news-learning-loop-audit-2026-08-21.ipynb`), never in a directory.
`snapshots/` and `research/` are the two exceptions, and neither holds a
notebook: one holds data, the other holds code too large for one file.

| Notebook | Channel | Question |
| --- | --- | --- |
| [`news-gepa-frozen-run-evaluation.ipynb`](news-gepa-frozen-run-evaluation.ipynb) | B | Does one frozen GEPA run form a consistent experiment, and may its candidate proceed? |
| [`news-learning-loop-audit-2026-08-21.ipynb`](news-learning-loop-audit-2026-08-21.ipynb) | C | What does the fixed 2026-08-21 24 h window actually say about the learning plane? |
| [`trading-agent-72h-event-study.ipynb`](trading-agent-72h-event-study.ipynb) | A | Over 72 h of delivered pushes, would a trading agent have had anything executable? |
| [`oi_chain_backtest_2026_09_03.py`](oi_chain_backtest_2026_09_03.py) | A | What did the deployed OI chain do to the 310 frames it saw, and were those frames worth anything? |
| [`research/oi_research_cli.py`](research/oi_research_cli.py) | A | Does the pre-registered #459 rule survive a holdout over every USDT perpetual Binance lists? |

A plain `.py` belongs here on the same terms as a notebook when the work has no
reason to carry cell state: same header block, same channel, same red lines. It
is a script rather than a notebook because its whole output is one receipt file
that `docs/research/` cites, and a committed `.ipynb` of a channel-A run would
have to be stripped of exactly that output. `make check` reads only `.ipynb`, so
a `.py` here answers to `ruff check` and `ruff format` instead.

## `research/` — the #459 Stage A open-interest program

One question needed more than one file, so it gets a directory rather than a
1,000-line script. #537 PR-1 moved it out of the service package, where it had
been sitting as `tracefold/trading/research/` with a `tracefold trading
oi-corpus|oi-replay` CLI in front of it: nothing in production ever imported it,
and a research corpus reader is not a service module.

```text
notebooks/research/
  oi_research_cli.py        the entry point; carries the header block for all of it
  oi_corpus.py              the sealed corpus format: content-addressed payloads, a fixed window
  oi_replay.py              the pre-registered rule and its measurement conventions
  open_interest_history.py  the Binance history walk, with the two rate budgets it needs
  test_oi_research.py       the self-tests: the four ways the replay's number could quietly lie
```

Two commands, never one. Collection is a fact about a 30-day Binance window that
cannot be served again; evaluation is a claim about a rule. Folding them
together would make every re-scoring silently re-collect, and no receipt could
name its own data.

```bash
# Seal a corpus (resumable; the window is fixed by the first pull).
uv run python notebooks/research/oi_research_cli.py oi-corpus pull --out ~/.tracefold/research/oi_corpus/<name>
# Re-seal the manifest from an existing progress log.
uv run python notebooks/research/oi_research_cli.py oi-corpus seal --out ~/.tracefold/research/oi_corpus/<name>
# Score the pre-registered rule; writes <corpus>/replay_receipt.json unless --out says otherwise.
uv run python notebooks/research/oi_research_cli.py oi-replay --corpus ~/.tracefold/research/oi_corpus/<name>
```

The modules import each other as siblings, so the entry script and the tests put
their own directory on `sys.path`; nothing here imports `tracefold`. Corpora and
receipts live under the operator's `~/.tracefold/`, never in the repository —
`docs/research/oi-stage-a-holdout-2026-09-01.md` quotes the corpus digest instead
of carrying the 5 GB it names.

The self-tests are not in `tests/`, so the fixed CI job set does not run them
(`testpaths = ["tests"]`): this is research, and CI verifies the service. Run
them beside the scripts, and before quoting any number they produced:

```bash
uv run python -m pytest notebooks/research/test_oi_research.py
```

## The three data channels

A notebook uses exactly one of these, and declares which in its first cell.
There is no fourth channel: anything else is a request to add a CLI command or
an HTTP surface, not a notebook.

### A — live read-only

Read the running system: HTTP `/api/*` on the operator's own service, or a
`tracefold` SQL session explicitly configured read-only. Public market data
(Binance, Yahoo) is part of this channel. Answers *what is the system doing
now*.

A live read has no fixed identity, so the notebook has to supply one: print the
UTC cut-off it actually queried, and pin the window the conclusions describe.
`trading-agent-72h-event-study.ipynb` shows the shape — it stores
`STUDY_AS_OF_UTC` and refuses to run at all once `now` has passed it. The bound
is the window's *start*: `/api/news/feed?hours=72` serves `[now - 72 h, now]`
and takes no cut-off parameter, so one second after the pinned moment the feed
has already dropped the oldest slice of the window the prose describes. A
notebook that would quietly report a different population under unchanged prose
is worse than one that stops.

### B — frozen artifact

Read an operator-owned learning-ledger artifact by sha: a dataset manifest, a
readiness report, a whole run directory. No database, no model endpoint. Answers
*what did this experiment show* — and because the inputs are immutable, the
answer can be archived and recomputed.

The path is injected through an environment variable, never hard-coded and never
committed:

```python
run_root = Path(os.environ["TRACEFOLD_GEPA_RUN_ROOT"]).expanduser().resolve()
```

Frozen datasets and evaluation reports are not repository content. They stay
under the operator's `~/.tracefold/`, and the notebook records their sha256 in
its own output rather than copying them in.

### C — committed snapshot

Commit the pair that makes a number reproducible forever: the `.sql` that
produced it and the `.json` it produced, both under `snapshots/`. The notebook
reads only the `.json`. Answers *the audit baseline we will keep citing*.

This is the only channel whose data lives in the repository, so it is the only
one bound by the redaction rules below. Use it when a number has to survive the
window that produced it — a fixed audit, a before/after baseline.

## Red lines

- **Read-only sessions only.** SQL uses the operator DSN with
  `default_transaction_read_only=on` (or an explicit read-only transaction).
  The shared login is write-capable, so the session policy—not a retired role
  name—is the safety boundary.
- **No business writes.** No Review acceptance, dataset freeze, candidate
  registration, promotion, canary arming, or order placement — not even through
  a CLI call in a cell.
- **No credentials in the notebook.** Connect the way the existing three do: a
  local HTTP endpoint, a DSN read from the operator's own config, or plain
  files. A bootstrap token stays in memory and is never printed or persisted.
- **No provider text or reader cards in `snapshots/`.** Committed snapshots are
  aggregates and identities. Follow the `docs/SECURITY.md` redaction allowlist —
  counts, shas, versions, windows, coverage; not headlines, not card bodies.
- **The runtime config is the operator's.** Live-data work follows the same
  discipline as everything else: `uv run tracefold config` must report
  `~/.tracefold/config.yaml`, and secret values are never printed.

## Setup

```bash
uv sync --group research
```

`research` is a non-default dependency group (`jupyterlab`, `ipykernel`,
`nbclient`, `nbconvert`, `pandas`, `matplotlib`). `make test`, `uv run` and the
Docker image never install it, so the Jupyter stack cannot reach a worker or a
release path.

One sharp edge: `uv sync` is *exact*, so a later plain `uv sync` — including the
one inside `make sync` — removes the research group again. `uv run` does not
prune, so an already-synced environment survives ordinary work; just re-run
`uv sync --group research` after a `make sync`.

## Run

Interactively — the kernel is the project venv, so `import tracefold` works:

```bash
uv run jupyter lab notebooks/
```

Use **Restart Kernel and Run All Cells**. A notebook whose cells were run out of
order is not evidence of anything.

Headless, which is also how a channel-B or channel-C run is reproduced:

```bash
# B — the run directory is named by the environment, not by the notebook.
# It must carry development*.json, readiness*.json, baseline-compile-live.json,
# optimization/optimization_report.json and an operator-written research-caveats.json;
# the notebook refuses the run rather than reporting a partial one.
TRACEFOLD_GEPA_RUN_ROOT=~/.tracefold/runs/<run> \
  uv run jupyter execute notebooks/news-gepa-frozen-run-evaluation.ipynb

# C — offline, in place, and byte-stable: an empty `git diff` afterwards *is* the
# reproduction result. Not `jupyter execute --inplace`: nbclient stamps per-cell
# wall-clock into metadata.execution with no way to switch it off from that CLI, so
# every run would dirty the file and a timestamp change would look like an evidence change.
uv run jupyter nbconvert --to notebook --execute --inplace \
  --ExecutePreprocessor.record_timing=False \
  notebooks/news-learning-loop-audit-2026-08-21.ipynb
git diff --stat -- notebooks/news-learning-loop-audit-2026-08-21.ipynb   # expect: nothing
```

Both run with the notebook's own directory as the working directory, so
channel-C notebooks address their data as `snapshots/<name>.json`. That is all
the parameterisation there is: environment variables in, no papermill, no
scheduler.

## The header block every notebook declares

The first cell is markdown and opens with a fenced `yaml` block carrying exactly
five keys. Copy this:

````markdown
# <Title>

```yaml
channel: B  # A live read-only | B frozen artifact | C committed snapshot
purpose: "The question this answers, and what it deliberately does not answer."
window: "The observation window, and where it comes from — a frozen artifact, a snapshot field, or a pinned constant."
identity: "The shas and versions the conclusions are bound to: bundle, program, policy, dataset, prompt, metric — whichever apply."
safety: "What is read, what is never touched, and what happens to credentials."
```
````

Prose below it, as much as the work needs. The block is what a reader — and
`make check` — reads first, because a research number without its window and its
identity is not a finding.

## Committing outputs

| Channel | Commit outputs? | Why |
| --- | --- | --- |
| A live read-only | **No** — strip before committing | The output cannot be reproduced; a stale one reads as current |
| B frozen artifact | **No** — strip before committing | The output belongs to the run directory, which is not repository content |
| C committed snapshot | **Yes** | The output *is* the evidence, and anyone with the repository can recompute it |

Stripping is `Kernel → Restart Kernel and Clear Outputs of All Cells`, then save.

## What `make check` enforces

`tests/architecture/test_research_notebooks.py`, over tracked files only:

- no tracked `.ipynb` outside `notebooks/`, and none in a subdirectory of it;
- a parseable five-key header block on the first cell, with `channel` in
  `{A, B, C}`;
- channel A and B notebooks carry no outputs and no execution counts;
- channel C notebooks carry outputs, and their code cells are numbered 1..n in
  document order — the signature of one Run All from a fresh kernel;
- channel C notebooks reach nothing outside the repository: no `urlopen`,
  `urllib.request`, `httpx`, `requests.`, `aiohttp` or `psycopg` in a code cell;
- no notebook commits `metadata.execution`, the per-cell wall clock of the run
  that produced it;
- no notebook code cell names a retired `tracefold_owner`, `tracefold_serve`,
  `tracefold_workers`, or `tracefold_nautilus` role.

Tracked files only, deliberately: an untracked notebook on the operator's disk
is a draft. It does not fail `make check`, and — by the same reasoning — it does
not fail `make deploy-image` either.
