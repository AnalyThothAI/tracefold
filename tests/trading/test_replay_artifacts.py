from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tracefold.app.cli.replay_artifacts import publish_replay_artifact, verify_replay_artifact
from tracefold.trading import BlacklistSnapshotV1, ReplayArtifactV1, ReplaySpecV1
from tracefold.trading.contracts import canonical_sha256


def _spec() -> ReplaySpecV1:
    return ReplaySpecV1(
        start_ms=1,
        end_ms=2,
        source_query_contract_sha256="1" * 64,
        source_facts_sha256=canonical_sha256([]),
        market_slice_sha256=canonical_sha256([]),
        research_universe_sha256="4" * 64,
        execution_capability_snapshot_sha256="5" * 64,
        replay_scenarios_sha256="6" * 64,
        blacklist_snapshot_sha256=BlacklistSnapshotV1(revision=0, active_rows=()).snapshot_sha256,
        strategy_identities=[{"strategy_id": "oi", "strategy_identity": "7" * 64}],
        intent_policy_sha256="8" * 64,
        execution_policy_sha256="9" * 64,
        app_revision="revision-1",
        app_image_digest="image-1",
        nautilus_wheel_identity="wheel-1",
        venue_scenarios=[{"venue": "binance.perp", "mode": "source_native"}],
        fee_model={"version": "fee-v1"},
        funding_model={"version": "unavailable-v1"},
        fill_model={"version": "bar-v1"},
        slippage_model={"version": "bar-v1"},
        latency_model={"version": "bar-v1"},
    )


def _artifact() -> ReplayArtifactV1:
    spec = _spec()
    return ReplayArtifactV1(
        run_id=spec.run_id,
        spec=spec,
        blacklist_snapshot_payload=BlacklistSnapshotV1(revision=0, active_rows=()),
        source_facts=[],
        market_slices=[],
        outcomes=[],
        summary={"source_count": 0, "funding": None},
    )


def test_replay_identity_contains_no_run_clock_and_is_reproducible() -> None:
    first = _spec()
    second = _spec()

    assert "created_at_ms" not in first.model_dump()
    assert first.run_id == second.run_id


def test_artifact_publish_is_atomic_idempotent_and_detects_corruption(tmp_path: Path) -> None:
    artifact = _artifact()
    path, digest = publish_replay_artifact(tmp_path, artifact)

    assert path == tmp_path / artifact.run_id / "replay.json"
    assert publish_replay_artifact(tmp_path, artifact) == (path, digest)
    verify_replay_artifact(path, expected_sha256=digest)

    path.write_text("corrupt", encoding="utf-8")
    with pytest.raises(RuntimeError, match="replay_artifact_corrupt"):
        publish_replay_artifact(tmp_path, artifact)


def test_concurrent_publish_verifies_the_winning_directory(monkeypatch, tmp_path: Path) -> None:
    artifact = _artifact()

    def lose_race(source: Path, destination: Path) -> None:
        shutil.copytree(source, destination)
        raise OSError("simulated directory publication race")

    monkeypatch.setattr("tracefold.app.cli.replay_artifacts.os.replace", lose_race)
    path, digest = publish_replay_artifact(tmp_path, artifact)

    verify_replay_artifact(path, expected_sha256=digest)
