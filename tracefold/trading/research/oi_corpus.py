"""Sealed open-interest corpus format: how a window is written, read back and proven.

Binance keeps `/futures/data/openInterestHist` for 30 days only, so a corpus of that window can never
be re-pulled: whatever is on disk is the only copy there will ever be of the data an issue was closed
on. That is the whole reason this file exists as a format rather than as a script's side effect.

* Every provider payload is stored **content-addressed** under the sha256 of its canonical bytes, and
  `read_payload` re-hashes on the way out. A later edit -- accidental or otherwise -- cannot pass as
  the sealed original.
* The window is fixed by the first write and reused on resume, so a pull that dies at 90% and is
  restarted an hour later cannot silently slide the corpus forward and score two different windows
  as one.
* `progress.jsonl` is appended and fsynced per symbol, which is what makes the resume possible.

No network and no argparse live here: the provider walk is
`tracefold.integrations.venues.open_interest_history`, and the operator command is
`tracefold trading oi-corpus`.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

FIVE_MIN_MS: Final = 300_000
HOUR_MS: Final = 3_600_000
DAY_MS: Final = 86_400_000

# Binance rejects `openInterestHist` beyond 30 days with -1130; 29 keeps the whole window inside the
# retention even when the pull itself takes an hour.
CORPUS_DAYS: Final = 29
# The replay needs an hour of candles before the first open-interest point (`pre1h`) and four hours
# after the last one (the forward return), so the candle window is wider on both ends.
CANDLE_LEAD_MS: Final = 2 * HOUR_MS
CANDLE_TAIL_MS: Final = 5 * HOUR_MS

DEFAULT_CORPUS_ROOT: Final = Path.home() / ".tracefold" / "research" / "oi_corpus"

CORPUS_ARTIFACT: Final = "SOURCE_FEATURE_DISCOVERY_CORPUS_V1_SEALED"


class CorpusError(RuntimeError):
    """A sealed corpus that cannot be trusted: a payload no longer hashes to its manifest digest."""


@dataclass(frozen=True, slots=True)
class CorpusWindow:
    """The five-minute grid every symbol in one corpus is pulled against."""

    start_ms: int
    end_ms: int

    @property
    def candle_start_ms(self) -> int:
        return self.start_ms - CANDLE_LEAD_MS

    @property
    def candle_end_ms(self) -> int:
        return self.end_ms + CANDLE_TAIL_MS

    def as_json(self) -> dict[str, int]:
        return {
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "candle_start_ms": self.candle_start_ms,
            "candle_end_ms": self.candle_end_ms,
        }


def window_now(*, days: int = CORPUS_DAYS, now_ms: int) -> CorpusWindow:
    """The window ending at the last complete five-minute boundary at or before `now_ms`."""

    end = now_ms // FIVE_MIN_MS * FIVE_MIN_MS
    return CorpusWindow(start_ms=end - days * DAY_MS, end_ms=end)


@dataclass(frozen=True, slots=True)
class SymbolRecord:
    """What one symbol contributed, and where its two payloads are."""

    symbol: str
    oi_sha256: str
    oi_points: int
    oi_first_ms: int | None
    oi_last_ms: int | None
    candle_sha256: str
    candle_points: int
    candle_first_ms: int | None
    candle_last_ms: int | None
    stored_bytes: int
    pulled_at_ms: int

    def as_json(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "oi_sha256": self.oi_sha256,
            "oi_points": self.oi_points,
            "oi_first_ms": self.oi_first_ms,
            "oi_last_ms": self.oi_last_ms,
            "candle_sha256": self.candle_sha256,
            "candle_points": self.candle_points,
            "candle_first_ms": self.candle_first_ms,
            "candle_last_ms": self.candle_last_ms,
            "stored_bytes": self.stored_bytes,
            "pulled_at_ms": self.pulled_at_ms,
        }


def canonical_bytes(payload: Any) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def write_payload(raw_root: Path, payload: Any) -> tuple[str, int]:
    """Store one raw provider payload under its own digest; return (sha256, stored bytes).

    `mtime=0` on the gzip header so the same payload is the same bytes on every machine and the
    digest names the content rather than the moment it was written.
    """

    body = canonical_bytes(payload)
    digest = hashlib.sha256(body).hexdigest()
    target = raw_root / f"{digest}.json.gz"
    if not target.exists():
        tmp = target.with_suffix(".tmp")
        tmp.write_bytes(gzip.compress(body, mtime=0))
        os.replace(tmp, target)
    return digest, target.stat().st_size


def read_payload(corpus_dir: Path, sha256: str) -> Any:
    """Read one sealed payload back, verifying it still hashes to the manifest's digest."""

    body = gzip.decompress((corpus_dir / "raw" / f"{sha256}.json.gz").read_bytes())
    actual = hashlib.sha256(body).hexdigest()
    if actual != sha256:
        raise CorpusError(f"corpus payload {sha256} hashes to {actual}")
    return json.loads(body)


def load_progress(corpus_dir: Path) -> dict[str, dict[str, Any]]:
    path = corpus_dir / "progress.jsonl"
    if not path.exists():
        return {}
    done: dict[str, dict[str, Any]] = {}
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            # A torn last line is the normal shape of a killed pull; the symbol is simply re-pulled.
            continue
        if isinstance(row, dict) and "symbol" in row:
            done[str(row["symbol"])] = row
    return done


def append_progress(corpus_dir: Path, record: SymbolRecord) -> None:
    with (corpus_dir / "progress.jsonl").open("a") as handle:
        handle.write(json.dumps(record.as_json(), sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def fix_window(corpus_dir: Path, window: CorpusWindow) -> CorpusWindow:
    """Persist the window on first use and return the persisted one on every later call."""

    path = corpus_dir / "window.json"
    if path.exists():
        stored = json.loads(path.read_text())
        return CorpusWindow(start_ms=int(stored["start_ms"]), end_ms=int(stored["end_ms"]))
    path.write_text(json.dumps(window.as_json(), indent=2, sort_keys=True) + "\n")
    return window


def store_universe(corpus_dir: Path, symbols: tuple[str, ...], *, now_ms: int) -> tuple[str, ...]:
    path = corpus_dir / "universe.json"
    if path.exists():
        return tuple(json.loads(path.read_text())["symbols"])
    path.write_text(json.dumps({"symbols": list(symbols), "captured_at_ms": now_ms}, indent=2, sort_keys=True) + "\n")
    return symbols


def seal(corpus_dir: Path, *, now_ms: int) -> dict[str, Any]:
    """Write `manifest.json` from the completed progress log and return it."""

    window_json = json.loads((corpus_dir / "window.json").read_text())
    universe = json.loads((corpus_dir / "universe.json").read_text())
    progress = load_progress(corpus_dir)
    records = [progress[symbol] for symbol in sorted(progress)]
    with_oi = [row for row in records if row.get("oi_points")]
    manifest = {
        "artifact": CORPUS_ARTIFACT,
        "issue": 459,
        "stage": "A",
        "venue": "binance_usdm",
        "endpoints": {
            "open_interest": "/futures/data/openInterestHist?period=5m",
            "candles": "/fapi/v1/klines?interval=5m",
            "universe": "/fapi/v1/exchangeInfo",
        },
        "window": window_json,
        "universe": universe,
        "symbols": records,
        "coverage": {
            "symbols_requested": len(universe["symbols"]),
            "symbols_stored": len(records),
            "symbols_with_open_interest": len(with_oi),
            "open_interest_points": sum(int(row.get("oi_points", 0)) for row in records),
            "candle_points": sum(int(row.get("candle_points", 0)) for row in records),
            "stored_bytes": sum(int(row.get("stored_bytes", 0)) for row in records),
            "expected_points_per_symbol": (window_json["end_ms"] - window_json["start_ms"]) // FIVE_MIN_MS,
        },
    }
    # The digest names the corpus, not the moment it was sealed. `sealed_at_ms` is recorded beside it
    # and deliberately excluded: a content address that changes when nothing changed would make every
    # re-seal look like a different corpus, and every receipt quoting the old value look forged.
    manifest["manifest_sha256"] = hashlib.sha256(canonical_bytes(manifest)).hexdigest()
    manifest["sealed_at_ms"] = now_ms
    (corpus_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


def dated_corpus_dir(*, now_ms: int, root: Path = DEFAULT_CORPUS_ROOT) -> Path:
    stamp = time.strftime("%Y%m%d", time.gmtime(now_ms / 1000))
    return root / f"binance-usdm-5m-{stamp}"


__all__ = [
    "CANDLE_LEAD_MS",
    "CANDLE_TAIL_MS",
    "CORPUS_ARTIFACT",
    "CORPUS_DAYS",
    "DAY_MS",
    "DEFAULT_CORPUS_ROOT",
    "FIVE_MIN_MS",
    "HOUR_MS",
    "CorpusError",
    "CorpusWindow",
    "SymbolRecord",
    "append_progress",
    "canonical_bytes",
    "dated_corpus_dir",
    "fix_window",
    "load_progress",
    "read_payload",
    "seal",
    "store_universe",
    "window_now",
    "write_payload",
]
