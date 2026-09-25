"""Pending shadow cases retain contemporaneous executable quotes across scans."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from tracefold.app import trading_analysis
from tracefold.app.analysis_files import AnalysisFiles


def test_shadow_quote_tape_appends_archived_samples(tmp_path, monkeypatch) -> None:
    now = 120_000
    monkeypatch.setattr(trading_analysis, "_clock_ms", lambda: now)
    files = AnalysisFiles(tmp_path)
    row = {
        "case_id": "case-1",
        "quote_tape_ref": None,
        "next_quote_at_ms": now,
        "target_selection": {
            "instrument": {
                "native_symbol": "SOLUSDT",
                "environment": "live",
                "mapping_semantics_digest": "mapping-1",
                "units_per_contract": "1",
            }
        },
    }
    calls = 0

    class Repo:
        def due_shadow_quote_samples(self, *, now_ms, limit):
            return [dict(row)] if row["next_quote_at_ms"] <= now_ms else []

        def record_shadow_quote_sample(self, *, case_id, prior_ref, tape_ref, sampled_at_ms):
            assert case_id == "case-1" and row["quote_tape_ref"] == prior_ref
            row["quote_tape_ref"] = tape_ref
            row["next_quote_at_ms"] = sampled_at_ms + 60_000
            return True

    async def db_async(call, **_kwargs):
        return call(SimpleNamespace(trading=Repo()))

    async def quote(_case):
        nonlocal calls
        calls += 1
        return {
            "status": "ok",
            "environment": "live",
            "native_symbol": "SOLUSDT",
            "mapping_semantics_digest": "mapping-1",
            "units_per_contract": "1",
            "payload": ({"received_at_ms": now, "bid": "99", "ask": "101", "bid_quantity": "2", "ask_quantity": "3"},),
        }

    runner = SimpleNamespace(_db_async=db_async, _read_executable_quote=quote, files=files)
    assert asyncio.run(trading_analysis.AnalysisRunner.sample_shadow_quotes_once(runner)) == 1
    assert asyncio.run(trading_analysis.AnalysisRunner.sample_shadow_quotes_once(runner)) == 0
    assert calls == 1
    now += 60_000
    assert asyncio.run(trading_analysis.AnalysisRunner.sample_shadow_quotes_once(runner)) == 1
    tape = files.read(row["quote_tape_ref"])
    assert len(tape["samples"]) == 2
    assert [sample["received_at_ms"] for sample in tape["samples"]] == [120_000, 180_000]
    assert all(sample["mapping_semantics_digest"] == "mapping-1" for sample in tape["samples"])
    assert all(files.read(sample["quote_ref"])["status"] == "ok" for sample in tape["samples"])
