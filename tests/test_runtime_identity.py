"""The deployed binary's identity, as a release receipt is allowed to claim it.

Four `news_agent_runtime_manifests` rows shipped with `image_digest = "unversioned"`
because the value was never plumbed anywhere: no compose environment, no build arg,
only `os.getenv(..., "unversioned")` in the composition root.  #121 cannot close on
a receipt chain that never names the image, so these tests pin both halves of the
gap — the value must arrive, and an absent value must never be recorded as one.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from tracefold.news.artifact_identity import runtime_manifest_sha
from tracefold.platform.runtime_identity import (
    IMAGE_DIGEST_ENV,
    RUNTIME_REVISION_ENV,
    UNVERSIONED,
    runtime_identity,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]


def test_runtime_identity_reads_both_halves() -> None:
    identity = runtime_identity({IMAGE_DIGEST_ENV: "tracefold-app@sha256:" + "a" * 64, RUNTIME_REVISION_ENV: "b" * 40})
    assert identity.image_digest == "tracefold-app@sha256:" + "a" * 64
    assert identity.runtime_revision == "b" * 40


def test_empty_environment_value_is_unversioned_not_an_empty_identity() -> None:
    """Compose renders an unset `${TRACEFOLD_IMAGE_DIGEST:-}` as `""`, so the
    variable exists and `os.getenv(name, default)` never falls back.  Recording
    `""` would let a receipt claim an identity nobody can verify."""

    for value in ("", "   ", "\n"):
        identity = runtime_identity({IMAGE_DIGEST_ENV: value, RUNTIME_REVISION_ENV: value})
        assert identity.image_digest == UNVERSIONED
        assert identity.runtime_revision == UNVERSIONED


def test_absent_environment_variables_are_unversioned() -> None:
    assert runtime_identity({}) == (UNVERSIONED, UNVERSIONED)


def test_each_half_is_normalised_independently() -> None:
    partial = runtime_identity({IMAGE_DIGEST_ENV: "sha256:" + "c" * 64})
    assert partial.image_digest == "sha256:" + "c" * 64
    assert partial.runtime_revision == UNVERSIONED


def test_manifest_sha_changes_with_the_image_digest() -> None:
    """Two deployments of the same code from different images are different
    deployments; the manifest sha is what a release receipt cites."""

    common = {"stable_bundle_sha": "d" * 64, "candidate_shas": [], "runtime_revision": "e" * 40}
    first = runtime_manifest_sha(image_digest="sha256:" + "1" * 64, **common)
    second = runtime_manifest_sha(image_digest="sha256:" + "2" * 64, **common)
    assert first != second
    assert runtime_manifest_sha(image_digest="sha256:" + "1" * 64, **common) == first


def test_compose_passes_the_image_digest_to_the_app_services() -> None:
    """The image cannot hash itself at build time, so the digest has to arrive as
    start-time environment.  Asserting on the merged service, not on the anchor
    text, is what proves a service did not shadow it with its own `environment`."""

    compose = yaml.safe_load((_REPO_ROOT / "compose.yaml").read_text())
    for service in ("migrate", "serve", "workers", "nautilus"):
        environment = compose["services"][service].get("environment") or {}
        assert environment.get(IMAGE_DIGEST_ENV) == f"${{{IMAGE_DIGEST_ENV}:-}}", (
            f"{service} must receive the image digest at start time"
        )


def test_deployment_uses_local_image_id_not_registry_repo_digest(monkeypatch) -> None:
    from scripts.deploy import Deployment

    deployment = Deployment(environ={})
    calls = []
    image = "sha256:" + "a" * 64

    def run(*args, **kwargs):
        calls.append(args)
        return image

    monkeypatch.setattr(deployment, "run", run)
    assert deployment.image_id("built-image") == image
    assert calls == [("docker", "image", "inspect", "--format", "{{.Id}}", "built-image")]
    deployment.select_app_image(image)
    assert deployment.env[IMAGE_DIGEST_ENV] == image
    assert deployment.env["TRACEFOLD_APP_IMAGE"] == image
