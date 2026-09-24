"""Copy and verify legacy analysis refs from cache into the durable archive.

The source remains intact for rollback. Stop Analysis while running this tool.
"""

from __future__ import annotations

import json
from pathlib import Path

from tracefold.app.analysis_files import AnalysisFiles
from tracefold.platform.paths import app_home


def copy_legacy_archive(home: Path) -> dict[str, int]:
    old_root = home / "cache" / "trading-analysis"
    new_root = home / "archive" / "trading-analysis"
    if not old_root.exists():
        return {"verified": 0, "copied": 0}
    if old_root.is_symlink() or new_root.is_symlink():
        raise ValueError("analysis_archive_symlink_refused")
    source = AnalysisFiles(old_root)
    target = AnalysisFiles(new_root)
    verified = copied = 0
    for path in sorted(old_root.rglob("*.json")):
        if path.is_symlink() or not path.is_file():
            raise ValueError("analysis_archive_nonregular_refused")
        digest = path.stem
        if path.parent.name != digest[:2]:
            raise ValueError("analysis_archive_path_invalid")
        value = source.read(digest)
        destination = new_root / digest[:2] / path.name
        existed = destination.exists()
        if target.write(value) != digest:
            raise ValueError("analysis_archive_digest_mismatch")
        verified += 1
        copied += not existed
    return {"verified": verified, "copied": copied}


def main() -> int:
    print(json.dumps(copy_legacy_archive(app_home()), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
