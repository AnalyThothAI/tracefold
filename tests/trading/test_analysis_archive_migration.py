from __future__ import annotations

from scripts.migrate_trading_analysis_archive import copy_legacy_archive
from tracefold.app.analysis_files import AnalysisFiles


def test_copy_legacy_archive_preserves_refs_and_is_idempotent(tmp_path) -> None:
    source = AnalysisFiles(tmp_path / "cache" / "trading-analysis")
    ref = source.write({"case_id": "case-1", "value": "frozen"})
    assert copy_legacy_archive(tmp_path) == {"verified": 1, "copied": 1}
    target = AnalysisFiles(tmp_path / "archive" / "trading-analysis")
    assert target.read(ref) == source.read(ref)
    assert copy_legacy_archive(tmp_path) == {"verified": 1, "copied": 0}
