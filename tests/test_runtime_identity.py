"""The deployed binary's identity, as a release receipt is allowed to claim it.

Four `news_agent_runtime_manifests` rows shipped with `image_digest = "unversioned"`
because the value was never plumbed anywhere: no compose environment, no build arg,
only `os.getenv(..., "unversioned")` in the composition root.  #121 cannot close on
a receipt chain that never names the image, so these tests pin both halves of the
gap — the value must arrive, and an absent value must never be recorded as one.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from tracefold.app.learning_runtime import (
    IMAGE_DIGEST_ENV,
    RUNTIME_REVISION_ENV,
    UNVERSIONED,
    runtime_identity,
    runtime_manifest_sha,
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
    for service in ("migrate", "serve", "workers"):
        environment = compose["services"][service].get("environment") or {}
        assert environment.get(IMAGE_DIGEST_ENV) == f"${{{IMAGE_DIGEST_ENV}:-}}", (
            f"{service} must receive the image digest at start time"
        )


def test_make_up_computes_the_digest_from_the_image_it_built() -> None:
    makefile = (_REPO_ROOT / "Makefile").read_text()
    up = makefile.split("\nup:", 1)[1].split("\nstatus:", 1)[0]
    build_at = up.index("docker compose build migrate")
    digest_at = up.index(IMAGE_DIGEST_ENV)
    start_at = up.index("docker compose up -d")
    assert build_at < digest_at < start_at, "the digest must be read after the build and before the start"
    assert "docker compose config --images" in up, "ask compose which image it will run, do not rebuild the name"
    assert "docker image inspect --format '{{.Id}}'" in up, (
        "`.Id` is present in every image store; `RepoDigests` is empty for a never-pushed build under the "
        "classic store and populated under containerd, so choosing it would make the recorded identity — "
        "and every manifest_sha derived from it — depend on the host"
    )
    assert "WARNING" in up, "an empty digest must be announced, not silently normalised to unversioned"


def test_the_manifest_sha_and_the_row_it_covers_share_one_read() -> None:
    """`runtime_manifest_sha()` hashes the four values the row then records.  The
    original code read the environment once per use — four `os.getenv` calls for
    two values — so the sha and the fields it claims to cover could drift apart.
    Both must now come from the same `RuntimeIdentity`."""

    workers = (_REPO_ROOT / "src/tracefold/app/workers/__init__.py").read_text()
    manifest = workers.split("runtime_manifest={", 1)[1].split("\n            },", 1)[0]
    for field in ("image_digest", "runtime_revision"):
        assert manifest.count(f"{field}=identity.{field}") == 1, f"{field} must reach the sha from the identity"
        assert manifest.count(f'"{field}": identity.{field}') == 1, f"{field} must reach the row from the identity"
    assert "os.getenv" not in manifest and "os.environ" not in manifest


def test_no_environment_read_bypasses_the_normaliser() -> None:
    """A second reader is how `""` gets back in.

    Matching on the variable *name* would not catch it: the code this replaced
    was `os.getenv(_RUNTIME_REVISION_ENV, "unversioned")` — a module constant,
    not a literal.  The composition root reads no environment at all, so the
    honest guard is that it stays that way.
    """

    workers = (_REPO_ROOT / "src/tracefold/app/workers/__init__.py").read_text()
    assert re.search(r"os\.(getenv|environ)\b", workers) is None, (
        "the Workers composition root must read the environment only through runtime_identity()"
    )
