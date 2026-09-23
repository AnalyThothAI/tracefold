"""A committed Case reference can only point to a complete, verified artifact."""

from __future__ import annotations

import os

import pytest

from tracefold.app.analysis_files import AnalysisFiles


def test_atomic_archive_failure_leaves_no_visible_reference(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    files = AnalysisFiles(tmp_path)
    original_replace = os.replace

    def fail_replace(_source: str, _target: str) -> None:
        raise OSError("fixture_publish_failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="fixture_publish_failure"):
        files.write({"source": "frozen"})
    assert list(tmp_path.rglob("*.json")) == []
    assert list(tmp_path.rglob(".analysis-*")) == []

    monkeypatch.setattr(os, "replace", original_replace)
    ref = files.write({"source": "frozen"})
    assert files.read(ref) == {"source": "frozen"}
    target = tmp_path / ref[:2] / f"{ref}.json"
    target.write_text('{"source":"corrupted"}', encoding="utf-8")
    with pytest.raises(ValueError, match="analysis_file_digest_mismatch"):
        files.write({"source": "frozen"})
    with pytest.raises(ValueError, match="analysis_file_digest_mismatch"):
        files.read(ref)
