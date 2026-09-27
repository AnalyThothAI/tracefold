"""Read-only image/program identity, independent of database lifecycle or genesis."""

from __future__ import annotations

import re
from argparse import Namespace
from typing import Any

from tracefold.app.workers.wiring.news import configured_runtime_manifest_sha
from tracefold.platform.config.loader import load_settings
from tracefold.platform.runtime_identity import runtime_identity


def handle_runtime_manifest(_args: Namespace) -> tuple[int, dict[str, Any]]:
    settings = load_settings(require_ws_token=False)
    identity = runtime_identity()
    # The immutable image proves the actual bytes. A dirty source suffix is honest
    # provenance for a development build, never a claim that those bytes are committed.
    if not re.fullmatch(r"[0-9a-f]{40}(?:-dirty)?", identity.runtime_revision) or not re.fullmatch(
        r"sha256:[0-9a-f]{64}", identity.image_digest
    ):
        return 1, {"ok": False, "error": "runtime_manifest_image_identity_required"}
    return 0, {
        "ok": True,
        "data": {
            "runtime_manifest_sha": configured_runtime_manifest_sha(settings, identity=identity),
            "runtime_revision": identity.runtime_revision,
            "image_digest": identity.image_digest,
            "source_dirty": identity.runtime_revision.endswith("-dirty"),
        },
    }
