"""#126 hard cut: the OpenNews account is the only place that decides which Strategies feed News.

`news.opennews_strategy_ids` read like an allowlist but was a filter in the Receiver, and Tracefold sends no
subscription frame — so it was a second switch for a decision the provider account already owned, and the two
had drifted. What is left is one switch, in the provider's dashboard.

The load-bearing assertion is that nothing filters a frame by strategy id again. The Gate still reads `1353`
off an Event's own provider metadata to mark a listing/delisting frame; that is provenance, not configuration,
and it is deliberately not banned here.
"""

from __future__ import annotations

import inspect
from pathlib import Path

from tracefold.news import opennews
from tracefold.news.consumers import DeduperConsumer, OpenNewsReceiver, RecoveryRunner
from tracefold.platform.config.models import NewsSettings

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "tracefold"

RETIRED_TOKENS = (
    "opennews_strategy_ids",
    "configured_strategy_ids",
    "strategy_warnings",
    "OPENNEWS_STRATEGY_ID_LIMIT",
)


def test_settings_carry_no_strategy_allowlist() -> None:
    assert "opennews_strategy_ids" not in NewsSettings.model_fields
    # `extra="forbid"`: a stale key in the operator's config.yaml must fail loudly, not be ignored.
    assert NewsSettings.model_config["extra"] == "forbid"


def test_frame_parsers_take_no_strategy_filter() -> None:
    for parser in (opennews.parse_opennews_message, opennews.parse_opennews_strategy_hits):
        assert "strategy_ids" not in inspect.signature(parser).parameters, parser.__name__


def test_no_runtime_component_holds_a_strategy_allowlist() -> None:
    for component in (OpenNewsReceiver, DeduperConsumer, RecoveryRunner):
        assert "strategy_ids" not in inspect.signature(component.__init__).parameters, component.__name__


def test_retired_allowlist_vocabulary_is_gone_from_production_source() -> None:
    offenders = [
        f"{path.relative_to(SRC)}: {token}"
        for path in SRC.rglob("*.py")
        # Migrations are the record of the cut and name what they dropped.
        if "alembic" not in path.parts
        for token in RETIRED_TOKENS
        if token in path.read_text(encoding="utf-8")
    ]
    assert offenders == []
