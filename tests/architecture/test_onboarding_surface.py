from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MAKE_TARGET_RE = re.compile(r"^(?P<target>[a-zA-Z0-9_-]+):", re.MULTILINE)


def test_one_command_onboarding_has_one_public_lifecycle() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    targets = {match.group("target") for match in MAKE_TARGET_RE.finditer(makefile)}

    assert {"up", "status", "logs", "down"} <= targets
    assert {
        "docker-up",
        "docker-status",
        "docker-logs",
        "docker-down",
    }.isdisjoint(targets)
    assert "docker compose build migrate" in makefile
    assert makefile.count("docker compose build ") == 1
    stop_writer = "docker compose stop -t 40 workers serve"
    start_stack = "docker compose up -d --no-build --force-recreate --wait"
    assert stop_writer in makefile
    assert makefile.index(stop_writer) < makefile.index(start_stack)
    assert "docker compose up -d --no-build --force-recreate --wait" in makefile
    assert "--wait-timeout $(TRACEFOLD_COMPOSE_WAIT_SECONDS) migrate serve workers" in makefile
    assert "docker compose up -d --build --force-recreate" not in makefile
    assert "git rev-parse --verify HEAD 2>/dev/null || true" not in makefile


def test_status_rejects_a_still_running_migration(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_docker = bin_dir / "docker"
    fake_docker.write_text(
        """#!/bin/sh
set -eu
if [ "$1" = "info" ]; then exit 0; fi
if [ "$1" = "compose" ] && [ "$2" = "version" ]; then exit 0; fi
if [ "$1" = "compose" ] && [ "$2" = "ps" ]; then
  service=""
  for argument do service="$argument"; done
  if [ "$service" = "migrate" ]; then printf '%s\\n' migrate-id; else printf '%s\\n' "${service}-id"; fi
  exit 0
fi
if [ "$1" = "inspect" ]; then
  format="$3"
  case "$format" in
    *State.Status*) printf '%s\\n' running ;;
    *State.ExitCode*) printf '%s\\n' 0 ;;
    *State.Health*) printf '%s\\n' healthy ;;
  esac
  exit 0
fi
exit 1
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o700)
    fake_curl = bin_dir / "curl"
    fake_curl.write_text(
        """#!/bin/sh
url=''
for argument do url="$argument"; done
case "$url" in */) printf '<html></html>' ;; esac
""",
        encoding="utf-8",
    )
    fake_curl.chmod(0o700)
    fake_uv = bin_dir / "uv"
    fake_uv.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_uv.chmod(0o700)

    result = subprocess.run(
        ["make", "status"],
        cwd=ROOT,
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode != 0
    assert "migrate: state=running exit_code=0" in result.stderr


def test_readme_routes_fresh_clone_to_the_complete_stack() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "make up" in readme
    assert "make docker-up" not in readme
    assert "make sync\nmake init\nmake db-migrate\nmake serve" not in readme
    assert "rsshub.env" not in readme


def test_generated_operator_config_has_no_static_duplicate() -> None:
    assert not (ROOT / "config.example.yaml").exists()
