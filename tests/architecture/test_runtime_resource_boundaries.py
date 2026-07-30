from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "tracefold"


def test_runtime_code_has_no_default_or_nested_thread_pools():
    violations: list[str] = []
    for path in SRC.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        relative = str(path.relative_to(ROOT))
        owns_explicit_executor = relative in {
            "src/tracefold/app/runtime_resources.py",
            "src/tracefold/app/http/ws.py",
        }
        if "asyncio.to_thread" in source or ("ThreadPoolExecutor" in source and not owns_explicit_executor):
            violations.append(str(path.relative_to(ROOT)))

    assert violations == []


def test_worker_side_has_no_in_process_live_publisher() -> None:
    forbidden = (
        "EventPublisherProtocol",
        "on_live_market_update",
        "runtime.hub",
    )
    violations: list[str] = []
    for path in (
        *sorted((SRC / "market").rglob("*.py")),
        *sorted((SRC / "news").rglob("*.py")),
        *sorted((SRC / "macro").rglob("*.py")),
        SRC / "app" / "bootstrap.py",
        SRC / "app" / "workers.py",
    ):
        source = path.read_text(encoding="utf-8")
        if any(marker in source for marker in forbidden):
            violations.append(str(path.relative_to(ROOT)))

    assert violations == []
