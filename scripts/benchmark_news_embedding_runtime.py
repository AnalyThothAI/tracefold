"""Measure the offline encoder inside the application image and its actual cgroup limits.

No database, model download, LLM or business operation is performed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import resource
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tracefold.app.claim_embedding import ClaimEmbedder, validate_model_snapshot
from tracefold.news.claim_recall import CALIBRATION


def process_state() -> dict[str, int]:
    values = {}
    for line in Path("/proc/self/status").read_text().splitlines():
        key, _, value = line.partition(":")
        if key in {"VmRSS", "Threads"}:
            values[key] = int(value.split()[0])
    return values


def cgroup_limits() -> dict[str, str]:
    return {
        name: Path(f"/sys/fs/cgroup/{name}").read_text().strip()
        for name in ("memory.max", "cpu.max")
        if Path(f"/sys/fs/cgroup/{name}").exists()
    }


async def measure(cache_dir: Path) -> dict[str, Any]:
    baseline = process_state()
    lag_ms: dict[str, list[float]] = {"startup": [], "inference": [], "close": []}
    phase = "startup"
    stopped = asyncio.Event()

    async def heartbeat() -> None:
        while not stopped.is_set():
            sampled_phase = phase
            before = time.perf_counter()
            await asyncio.sleep(0.01)
            lag_ms[sampled_phase].append(max(0, (time.perf_counter() - before - 0.01) * 1000))

    ticker = asyncio.create_task(heartbeat())
    embedder = ClaimEmbedder(model=CALIBRATION.embedder.model, cache_dir=cache_dir)
    try:
        before = time.perf_counter()
        if not await embedder.self_test():
            raise RuntimeError("news_embedding_resource_self_test_failed")
        startup_ms = (time.perf_counter() - before) * 1000
        loaded = process_state()
        phase = "inference"
        # Every statement exceeds the fixed 256-token truncation boundary.
        texts = [
            f"Agency {index} reports tariffs. " + "industrial imports inflation markets " * 300 for index in range(32)
        ]
        batches_ms = []
        for _ in range(5):
            before = time.perf_counter()
            probes = await embedder.probes(texts)
            batches_ms.append((time.perf_counter() - before) * 1000)
            if len(probes) != len(texts) or any(
                probe.text != text or probe.vector is None or probe.embedder != CALIBRATION.embedder.key
                for text, probe in zip(texts, probes, strict=True)
            ):
                raise RuntimeError("news_embedding_resource_batch_failed")
        encoded = process_state()
        if encoded["Threads"] > baseline["Threads"] + 3:
            raise RuntimeError("news_embedding_native_threads_unbounded")
    finally:
        phase = "close"
        await embedder.aclose()
        stopped.set()
        await ticker
    return {
        "protocol": "799_application_image_resource_v1",
        "ok": True,
        "measured_at": datetime.now(UTC).isoformat(),
        "cgroup_limits": cgroup_limits(),
        "model_snapshot": validate_model_snapshot(cache_dir),
        "baseline": baseline,
        "loaded": loaded,
        "after_five_batches": encoded,
        "after_close": process_state(),
        "peak_rss_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024,
        "rss_model_delta_mib": (loaded["VmRSS"] - baseline["VmRSS"]) / 1024,
        "startup_golden_ms": startup_ms,
        "batch_size": 32,
        "tokens": CALIBRATION.embedder.max_tokens,
        "batch_ms": batches_ms,
        "event_loop_lag_ms_max": {key: max(values, default=0) for key, values in lag_ms.items()},
        "event_loop_samples": {key: len(values) for key, values in lag_ms.items()},
        "limits": (
            "Encoder-only process in the application image; "
            "production Workers coexistence and 24h load are not measured."
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(measure(args.cache_dir)), indent=2))


if __name__ == "__main__":
    main()
