"""The OpenNews account is the only place that decides which Strategies feed News.

`news.opennews_strategy_ids` read like an allowlist but was a filter in the Receiver, and Tracefold sends no
subscription frame — so it was a second switch for a decision the provider account already owned, and the two
had drifted. What is left is one switch, in the provider's dashboard.

The load-bearing assertion is that nothing filters a frame by strategy id again. One pure exact source-contract
table may interpret the complete normalized tuple as provenance; no receiver, Gate, worker or second module may
turn an id alone into enablement or a domain type.
"""

from __future__ import annotations

import inspect
from pathlib import Path

from tracefold.news.opennews import parse_opennews_message, parse_opennews_strategy_hits
from tracefold.news.pipeline.admission import DeduperConsumer
from tracefold.news.pipeline.receiver import OpenNewsReceiver
from tracefold.news.pipeline.recovery import RecoveryRunner
from tracefold.platform.config.models import NewsSettings

ROOT = Path(__file__).resolve().parents[2]
NEWS_ROOT = ROOT / "src" / "tracefold" / "news"
SOURCE_CONTRACT_PATH = NEWS_ROOT / "source_contracts.py"
BOUND_IDS = ("1019", "1353", "2000", "2026", "2083")


def test_settings_carry_no_strategy_allowlist() -> None:
    assert "opennews_strategy_ids" not in NewsSettings.model_fields
    # `extra="forbid"`: a stale key in the operator's config.yaml must fail loudly, not be ignored.
    assert NewsSettings.model_config["extra"] == "forbid"


def test_frame_parsers_take_no_strategy_filter() -> None:
    for parser in (parse_opennews_message, parse_opennews_strategy_hits):
        assert "strategy_ids" not in inspect.signature(parser).parameters, parser.__name__


def test_no_runtime_component_holds_a_strategy_allowlist() -> None:
    for component in (OpenNewsReceiver, DeduperConsumer, RecoveryRunner):
        assert "strategy_ids" not in inspect.signature(component.__init__).parameters, component.__name__


def test_exact_strategy_identity_is_owned_only_by_the_source_contract_classifier() -> None:
    offenders: list[str] = []
    for path in NEWS_ROOT.rglob("*.py"):
        if path == SOURCE_CONTRACT_PATH:
            continue
        text = path.read_text(encoding="utf-8")
        if any(f'"{strategy_id}"' in text or f"'{strategy_id}'" in text for strategy_id in BOUND_IDS):
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []
