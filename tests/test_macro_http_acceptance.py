from __future__ import annotations

import json

import httpx

from tracefold.app.macro_acceptance import collect_macro_http_acceptance
from tracefold.macro import MACRO_MODULE_IDS, build_typed_module_payload


def test_macro_http_acceptance_checks_all_current_modules_and_revalidation() -> None:
    payloads = {module_id: _available_payload(module_id) for module_id in MACRO_MODULE_IDS}
    requests: list[tuple[str, str | None]] = []

    with httpx.Client(
        base_url="https://tracefold.test",
        transport=httpx.MockTransport(_handler(payloads, requests)),
    ) as client:
        report = collect_macro_http_acceptance(client, auth_token="secret")

    assert report["status"] == "passed", report
    assert [module["module_id"] for module in report["modules"]] == list(MACRO_MODULE_IDS)
    assert report["issues"] == []
    assert sum(path != "/api/macro/overview" for path, _etag in requests) == 12


def test_macro_http_acceptance_fails_closed_for_degraded_current_health() -> None:
    payloads = {module_id: _available_payload(module_id) for module_id in MACRO_MODULE_IDS}
    payloads["credit"]["status"]["current_health"]["state"] = "degraded"
    with httpx.Client(
        base_url="https://tracefold.test",
        transport=httpx.MockTransport(_handler(payloads, [])),
    ) as client:
        report = collect_macro_http_acceptance(client, auth_token="secret")

    assert report["status"] == "failed"
    assert any(
        issue == {"path": "/api/macro/credit", "code": "current_health_not_current"} for issue in report["issues"]
    )


def _available_payload(module_id: str) -> dict[str, object]:
    payload = build_typed_module_payload(
        module_id=module_id,
        now_ms=1_785_000_000_000,
        series_rows=[],
        market_rows=[],
        position_rows=[],
        settlement_rows=[],
        release_rows=[],
        document_rows=[],
        target_states=[],
    )
    payload["status"]["coverage"]["state"] = "complete"
    payload["status"]["current_health"] = {
        "state": "current",
        "current_datasets": 0,
        "tracked_datasets": 0,
        "as_of_ms": 1_785_000_000_000,
        "groups": [],
    }
    payload["availability"] = "available"
    payload["reason"] = None
    if module_id == "rates_fed":
        payload["document_analysis_runtime"] = {
            "state": "disabled",
            "enabled": False,
            "configured": False,
            "worker_active": False,
            "model": "gpt-5.4-mini",
        }
    return payload


def _overview(payloads: dict[str, dict[str, object]]) -> dict[str, object]:
    modules = []
    for module_id in MACRO_MODULE_IDS:
        payload = payloads[module_id]
        modules.append(
            {
                "module_id": module_id,
                "label": payload["label"],
                "availability": "available",
                "reason": None,
                "coverage_state": "complete",
                "current_health_state": "current",
                "history_depth_state": payload["status"]["history_depth"]["state"],
                "backfill_execution": payload["status"]["backfill_execution"],
                "latest_fact_at_ms": payload["latest_fact_at_ms"],
                "summary": (
                    {
                        "headline": payload["decision"]["headline"],
                        "interpretation": None,
                    }
                    if module_id == "rates_fed"
                    else {
                        "headline": payload["summary"]["headline"],
                        "interpretation": payload["summary"]["interpretation"],
                    }
                ),
                "coverage_gap_count": 0,
                "current_health_gap_count": 0,
                "history_gap_count": 0,
                "href": f"/macro/{module_id.replace('_', '-')}",
            }
        )
    return {
        "schema_version": "macro_overview_v9",
        "read_at_ms": 1_785_000_000_000,
        "transport": {
            "state": "current",
            "last_successful_read_at_ms": 1_785_000_000_000,
            "reason": None,
        },
        "latest_fact_at_ms": 0,
        "modules": modules,
        "data_quality": {
            "coverage_state": "complete",
            "current_health_state": "current",
            "history_depth_state": "partial",
            "coverage_gap_count": 0,
            "current_health_gap_count": 0,
            "history_gap_count": 0,
        },
    }


def _module_id_for_path(path: str) -> str:
    route = path.removeprefix("/api/macro/").replace("-", "_")
    if route not in MACRO_MODULE_IDS:
        raise AssertionError(f"unexpected macro path: {path}")
    return route


def _handler(
    payloads: dict[str, dict[str, object]],
    requests: list[tuple[str, str | None]],
):
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.url.path, request.headers.get("if-none-match")))
        if request.url.path == "/api/macro/overview":
            return httpx.Response(200, json={"ok": True, "data": _overview(payloads)})
        module_id = _module_id_for_path(request.url.path)
        etag = f'"{module_id}-payload"'
        if request.headers.get("if-none-match") == etag:
            return httpx.Response(304, headers={"ETag": etag})
        return httpx.Response(
            200,
            headers={"ETag": etag, "Cache-Control": "private, no-cache"},
            content=json.dumps({"ok": True, "data": payloads[module_id]}).encode(),
        )

    return handler
