"""Wall clock used by News work and delivery audit timestamps."""

import time


def clock_ms() -> int:
    return time.time_ns() // 1_000_000
