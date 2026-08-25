"""The operator's research window: snapshot and compare.

Read-only against the database, file-backed, and structurally unable to propose anything. It shares the
metric and the episode projection with the release plane precisely so that what it measures predicts what
an optimization will do — and shares nothing else.

Until #202 this package also held `optimize` and an `ExperimentCandidate` marked `promotable=False`, a
second candidate lifecycle that existed only because a release candidate had to come out of a sealed
container. There is one optimization entry point now (`tracefold.news.learning.optimizer`) and one
candidate contract, so a research winner is registered rather than reproduced.
"""

from __future__ import annotations

__all__: list[str] = []
