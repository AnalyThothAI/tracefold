# Offline research workspace

[Current handbook](../docs/README.md) · [Review](../docs/modules/review.md) · [Trading](../docs/modules/trading.md)

Research files are outside production imports and account authority. This workspace
contains both reusable offline OI utilities and deliberately historical experiments.
A preserved notebook or old snapshot does not make its former runtime modules,
model-release commands or trading strategy current.

## 1. Inventory and execution status

| File / area | Use and limitation |
| --- | --- |
| [research/oi_research_cli.py](research/oi_research_cli.py) | Offline corpus/replay CLI. Inspect `--help` and supply its explicit inputs/output directory. |
| [oi_corpus.py](research/oi_corpus.py), [oi_replay.py](research/oi_replay.py), [open_interest_history.py](research/open_interest_history.py) | Pure research data/replay helpers; no account execution authority. |
| [test_oi_research.py](research/test_oi_research.py) | Hermetic checks for those offline utilities. |
| [oi_exit_rules_replay_2026_09_07.py](research/oi_exit_rules_replay_2026_09_07.py) | Historical fixed-input exit-rule study. Reads the preserved baseline and its declared candle-cache inputs; not a current live strategy or a model decision. |
| [oi_chain_backtest_2026_09_03.py](oi_chain_backtest_2026_09_03.py) | Historical producer tied to the source/schema of that date. It references retired Trading definitions and is **not** a supported current-main execution recipe. Use its original Git source/environment for reproduction. |
| [news-gepa-frozen-run-evaluation.ipynb](news-gepa-frozen-run-evaluation.ipynb) | Historical frozen-run analysis, not a currently available News GEPA optimizer. Old run artifacts and their matching code are required. |
| [news-learning-loop-audit-2026-08-21.ipynb](news-learning-loop-audit-2026-08-21.ipynb) | Committed historical audit inputs and outputs. Its findings concern the recorded window, not the new EventUpdate system. |
| [trading-agent-72h-event-study.ipynb](trading-agent-72h-event-study.ipynb) | Historical window-bound study. An expired source window must remain blocked rather than silently switching to today's data. |

The old narrative reports and one-off SQL/query-plan receipts are available at
their original Git revision; they are not duplicated in current `docs/`. A historical
source link remains pinned rather than changed to a newer file with different logic.

## 2. Preserved data has an actual consumer

| Input / result | Consumer and evidence meaning |
| --- | --- |
| [oi-chain-backtest-2026-09-03.json](snapshots/oi-chain-backtest-2026-09-03.json) | The fixed baseline read by the historical exit-rule replay. Moved here without changing bytes or data identity. |
| [oi-exit-rules-replay-2026-09-07.json](snapshots/oi-exit-rules-replay-2026-09-07.json) | Preserved output of that fixed study; not current performance or a live fill journal. |
| [News audit snapshot](snapshots/news-review-24h-audit-snapshot-2026-08-21.json) | The committed input of the historical News audit notebook. |
| [News audit SQL](snapshots/news-review-24h-audit-2026-08-21.sql) | Records how that snapshot was obtained under the historical schema. Do not run against a current production database by default. |

The two OI files previously lived under `docs/research`; their owning Python paths
now point here. A documentation move does not authorize rerunning an experiment
and overwriting its result. Output identities and observed numbers remain unchanged.
Frozen dataset prose elsewhere can name retired documentation as historical
provenance; do not rewrite dataset bytes merely to refresh those references.

## 3. Declared data channels

Every tracked notebook starts with one Markdown YAML declaration containing exactly
`channel`, `purpose`, `window`, `identity`, and `safety`.

| Channel | Data source | Committed outputs |
| --- | --- | --- |
| A | Explicit live/read-only window | None; a past moving window cannot be reproduced from today's database. |
| B | Operator-owned frozen run/artifact directory | None; the required artifacts are not implicitly repository data. |
| C | Inputs committed in this repository | Keep the outputs that constitute its reproducible evidence. |

Keep tracked `.ipynb` files flat under `notebooks/`. A/B notebooks have no committed
execution counts or outputs. C notebooks must reflect one in-order fresh-kernel
run and reach no network/database/provider outside their committed inputs. Avoid
per-cell execution wall-clock metadata that changes independently of evidence.
These conventions are enforced by
[research workspace tests](../tests/architecture/test_research_notebooks.py).

Do not rewrite a notebook's declared window, data channel or authority merely to
make a failed current-main run look valid. Historical schema/module incompatibility
is a recorded reproduction boundary, not permission to invent compatible facts.

## 4. Development and verification

Research dependencies are an explicit optional group, not part of the default
application setup or deployed image:

```bash
uv sync --group research
uv run python notebooks/research/oi_research_cli.py --help
uv run python -m pytest notebooks/research/test_oi_research.py
```

Review the specific script's input and output handling before execution. Do not
point an experiment at the operator's live database by default, enable execution,
read credential files into outputs or launch an unbounded model campaign.
Notebook output, historical backtest return, Trading decision and actual native
execution PnL remain different evidence.
