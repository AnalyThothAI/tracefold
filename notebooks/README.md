# Offline OI research

[Current handbook](../docs/README.md) · [Trading](../docs/modules/trading.md)

This workspace contains four reusable, read-only OI research modules. They have no account execution authority:

| Module | Current role |
| --- | --- |
| [open_interest_history.py](research/open_interest_history.py) | Bounded OI and candle history collection. |
| [oi_corpus.py](research/oi_corpus.py) | Deterministic offline corpus construction. |
| [oi_replay.py](research/oi_replay.py) | Replay over explicit, fixed research inputs. |
| [oi_research_cli.py](research/oi_research_cli.py) | CLI for an operator-provided corpus and output directory. |

The hermetic checks live in [tests/research/test_oi_research.py](../tests/research/test_oi_research.py) and run in the normal test collection. Run `uv run python notebooks/research/oi_research_cli.py --help` to inspect inputs; collection itself requires an explicit provider and window. The research tools do not grant trading permissions.

The old date-bound News and Trading notebooks, one-off OI studies, their snapshots, and the frozen News Gold copy were retired in [issue #736](https://github.com/AnalyThothAI/tracefold/issues/736). Retrieve their exact source and data from Git commit `364e0d9abdc5c1f2dcc27aa19c2bb0736b7fffaf` when reproducing a historical result. Those experiments do not describe the current EventUpdate or Trading decision contract.
