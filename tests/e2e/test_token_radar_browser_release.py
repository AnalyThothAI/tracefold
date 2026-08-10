from __future__ import annotations

import os
import signal
import subprocess
import sys
from contextlib import suppress

from tests.e2e.token_radar_release import _process_group_exists, _stop_process_group


def test_token_radar_browser_lane_cleans_descendant_processes() -> None:
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import subprocess, sys, time; "
                "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
                "print(child.pid, flush=True); time.sleep(60)"
            ),
        ],
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    assert process.stdout is not None
    child_pid = int(process.stdout.readline())
    try:
        _stop_process_group(process)
        with suppress(ProcessLookupError):
            os.kill(child_pid, 0)
            raise AssertionError(f"Playwright descendant {child_pid} survived process-group cleanup")
    finally:
        if _process_group_exists(process.pid):
            os.killpg(process.pid, signal.SIGKILL)


def test_token_radar_fact_becomes_visible_in_the_real_browser_within_one_minute(
    e2e_token_radar_browser_release,
) -> None:
    e2e_token_radar_browser_release()
