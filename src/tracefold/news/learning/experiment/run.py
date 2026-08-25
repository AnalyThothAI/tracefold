"""The operator's run directory: a frozen closed-window snapshot the fast loop works from.

This is research scaffolding, deliberately outside every release path. It reads the database once,
freezes what it read into files the operator owns, and never writes back: no verdict, no review, no
dataset, no candidate, no activation. Nothing here can promote anything, and the release plane cannot
read it — a run directory is not a second truth, it is a notebook that happens to be reproducible.

What makes it reproducible is that the cases are the *same* projection the trusted compiler seals. The
snapshot calls `CandidateEvaluator`'s own episode projection rather than re-deriving one, because two
projections would let the number an operator reads drift from the number a compile maximizes — which is
the failure `_project_episodes` exists to prevent, and the reason `run_gepa` is shared.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ...artifact_identity import canonical_json, canonical_sha

RUN_MANIFEST_SCHEMA: Literal["tracefold.news.experiment_run_manifest.v1"] = "tracefold.news.experiment_run_manifest.v1"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"


class _ExactModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ExperimentWindow(_ExactModel):
    """The closed window this run froze. Closed, because an open one is not reproducible."""

    from_ms: int = Field(ge=0)
    to_ms: int = Field(gt=0)

    @model_validator(mode="after")
    def _ordered(self) -> ExperimentWindow:
        if self.to_ms <= self.from_ms:
            raise ValueError("news_experiment_window_invalid")
        return self


class ExperimentRunManifest(_ExactModel):
    """What this run is, and what it may be compared against.

    The identity list is deliberately short — the experiment plane is allowed a run identity, a dataset
    identity, the parent Program it measured against, and the exact model identities. It carries no
    compile receipt, no candidate registration and no release evidence, because it produces none.
    """

    schema_version: Literal["tracefold.news.experiment_run_manifest.v1"] = RUN_MANIFEST_SCHEMA
    name: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    window: ExperimentWindow
    parent_program_sha256: str = Field(pattern=_SHA256_PATTERN)
    program_version: str = Field(min_length=1)
    policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    case_count: int = Field(ge=0)
    accepted_case_count: int = Field(ge=0)
    case_root_sha256: str = Field(pattern=_SHA256_PATTERN)
    created_at_ms: int = Field(ge=0)
    run_sha256: str = Field(pattern=_SHA256_PATTERN)

    @classmethod
    def issue(cls, **values: Any) -> ExperimentRunManifest:
        draft = cls.model_construct(run_sha256="0" * 64, **values)
        payload = draft.model_dump(mode="json", exclude={"run_sha256"})
        return cls(**values, run_sha256=canonical_sha(payload))

    @model_validator(mode="after")
    def _identity_is_exact(self) -> ExperimentRunManifest:
        if self.accepted_case_count > self.case_count:
            raise ValueError("news_experiment_accepted_case_count_invalid")
        if self.run_sha256 != canonical_sha(self.model_dump(mode="json", exclude={"run_sha256"})):
            raise ValueError("news_experiment_run_hash_mismatch")
        return self


class ExperimentCase(_ExactModel):
    """One frozen case: exactly what the Program would be asked, and what production answered.

    `case_sha256` is the evaluator's own case id, not a new one. It is the resume key, so a rerun that
    invents its own identity would resume against a corpus it never measured.
    """

    case_sha256: str = Field(min_length=1)
    cluster_id: str = Field(min_length=1)
    stratum: str = Field(min_length=1)
    # Kept beside the episode rather than inside it: `DevelopmentEpisode` forbids the key, and this is what
    # `draft-reviews --events-from` needs to ask the ReviewDesk for exactly the cases nobody has judged.
    event_id: str = Field(min_length=1)
    episode: dict[str, Any]
    accepted: bool

    @property
    def accepted_review(self) -> Mapping[str, Any]:
        return dict(self.episode.get("accepted_review") or {})


class ExperimentRun:
    """A directory on disk. Nothing here is a database, and nothing here may become one."""

    def __init__(self, root: Path) -> None:
        self.root = _safe_directory(root)

    @property
    def manifest_path(self) -> Path:
        return self.root / "manifest.json"

    @property
    def cases_dir(self) -> Path:
        return self.root / "cases"

    @property
    def compare_dir(self) -> Path:
        return self.root / "compare"

    def write_manifest(self, manifest: ExperimentRunManifest) -> None:
        _write_json(self.manifest_path, manifest.model_dump(mode="json"))

    def manifest(self) -> ExperimentRunManifest:
        if not self.manifest_path.is_file():
            raise ValueError("news_experiment_run_manifest_missing")
        return ExperimentRunManifest.model_validate(_read_json(self.manifest_path))

    def write_case(self, case: ExperimentCase) -> None:
        self.cases_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        _write_json(self.cases_dir / f"{case.case_sha256}.json", case.model_dump(mode="json"))

    def cases(self) -> Iterator[ExperimentCase]:
        """Every frozen case, in a fixed order so two runs of `compare` see the same sequence."""

        if not self.cases_dir.is_dir():
            return
        for path in sorted(self.cases_dir.glob("*.json")):
            yield ExperimentCase.model_validate(_read_json(path))

    def write_compared(self, case_sha256: str, payload: Mapping[str, Any]) -> None:
        self.compare_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        _write_json(self.compare_dir / f"{case_sha256}.json", dict(payload))

    def compared(self) -> dict[str, dict[str, Any]]:
        """What a previous `--resume` already answered, keyed by the case it answered for."""

        if not self.compare_dir.is_dir():
            return {}
        return {path.stem: _read_json(path) for path in sorted(self.compare_dir.glob("*.json"))}

    def write_report(self, name: str, payload: Mapping[str, Any]) -> Path:
        path = self.root / f"{name}.json"
        _write_json(path, dict(payload))
        return path


def case_root_sha256(cases: Sequence[ExperimentCase]) -> str:
    """One root over the frozen case identities, so a run cannot silently change what it measured."""

    return canonical_sha([case.case_sha256 for case in cases])


def _safe_directory(root: Path) -> Path:
    requested = Path(root)
    if ".." in requested.parts:
        raise ValueError("news_experiment_run_path_invalid")
    requested.mkdir(parents=True, exist_ok=True, mode=0o700)
    resolved = requested.resolve(strict=True)
    if requested.is_symlink() or not resolved.is_dir() or requested.absolute().resolve() != resolved:
        raise ValueError("news_experiment_run_path_invalid")
    return resolved


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    """Atomic and no-follow, so a half-written run directory is never mistaken for a complete one."""

    document = canonical_json(dict(value)) + "\n"
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        encoded = document.encode("utf-8")
        written = 0
        while written < len(encoded):
            written += os.write(descriptor, encoded[written:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _read_json(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"news_experiment_run_file_invalid:{path.name}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"news_experiment_run_file_invalid:{path.name}")
    return raw


__all__ = [
    "RUN_MANIFEST_SCHEMA",
    "ExperimentCase",
    "ExperimentRun",
    "ExperimentRunManifest",
    "ExperimentWindow",
    "case_root_sha256",
]
