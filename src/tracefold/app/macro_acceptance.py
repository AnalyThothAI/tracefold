from __future__ import annotations

from typing import Any

import httpx
from pydantic import TypeAdapter, ValidationError

from tracefold.app.http import schemas as api_schemas
from tracefold.app.http.macro_modules import MACRO_HTTP_MODULES

_OVERVIEW_ENVELOPE = TypeAdapter(api_schemas.ApiEnvelope[api_schemas.MacroOverviewReadData])


def collect_macro_http_acceptance(
    client: httpx.Client,
    *,
    auth_token: str,
) -> dict[str, Any]:
    """Verify the deployed Macro read interface without changing readiness."""
    headers = {"Authorization": f"Bearer {auth_token}"}
    issues: list[dict[str, str]] = []
    overview_summary: dict[str, Any] | None = None
    modules: list[dict[str, Any]] = []

    try:
        overview_response = client.get("/api/macro/overview", headers=headers)
        overview_response.raise_for_status()
        overview = _OVERVIEW_ENVELOPE.validate_python(overview_response.json())
        overview_data = overview.data
        if overview_data is None:
            raise ValueError("macro_overview_missing_data")
        overview_summary = {
            "coverage_state": overview_data.data_quality.coverage_state,
            "current_health_state": overview_data.data_quality.current_health_state,
            "history_depth_state": overview_data.data_quality.history_depth_state,
        }
        if overview_data.data_quality.coverage_state != "complete":
            _issue(issues, "/api/macro/overview", "coverage_incomplete")
        if overview_data.data_quality.current_health_state != "current":
            _issue(issues, "/api/macro/overview", "current_health_not_current")
    except (httpx.HTTPError, ValidationError, ValueError, TypeError) as exc:
        _issue(issues, "/api/macro/overview", "overview_read_invalid", exc)

    for contract in MACRO_HTTP_MODULES:
        module_summary: dict[str, Any] = {
            "module_id": contract.module_id,
            "path": contract.api_path,
            "availability": "unavailable",
            "coverage_state": None,
            "current_health_state": None,
            "history_depth_state": None,
            "etag_revalidated": False,
        }
        try:
            response = client.get(contract.api_path, headers=headers)
            response.raise_for_status()
            envelope = contract.read_envelope.validate_python(response.json())
            data = envelope.data
            if data is None:
                raise ValueError("macro_module_missing_data")
            if data.module_id != contract.module_id:
                raise ValueError("macro_module_identity_mismatch")
            module_summary["availability"] = data.availability
            if isinstance(data, api_schemas.MacroModuleUnavailableData):
                _issue(issues, contract.api_path, "module_unavailable")
            else:
                module_summary.update(
                    {
                        "coverage_state": data.status.coverage.state,
                        "current_health_state": data.status.current_health.state,
                        "history_depth_state": data.status.history_depth.state,
                    }
                )
                if data.status.coverage.state != "complete":
                    _issue(issues, contract.api_path, "coverage_incomplete")
                if data.status.current_health.state != "current":
                    _issue(issues, contract.api_path, "current_health_not_current")

            etag = response.headers.get("etag")
            if not etag or response.headers.get("cache-control") != "private, no-cache":
                _issue(issues, contract.api_path, "revalidation_headers_missing")
            else:
                unchanged = client.get(
                    contract.api_path,
                    headers={**headers, "If-None-Match": etag},
                )
                if unchanged.status_code != 304 or unchanged.content:
                    _issue(issues, contract.api_path, "conditional_read_failed")
                elif unchanged.headers.get("etag") != etag:
                    _issue(issues, contract.api_path, "conditional_etag_mismatch")
                else:
                    module_summary["etag_revalidated"] = True
        except (httpx.HTTPError, ValidationError, ValueError, TypeError) as exc:
            _issue(issues, contract.api_path, "module_read_invalid", exc)
        modules.append(module_summary)

    return {
        "schema_version": "macro_http_acceptance_v1",
        "status": "passed" if not issues else "failed",
        "overview": overview_summary,
        "modules": modules,
        "issues": issues,
    }


def _issue(
    issues: list[dict[str, str]],
    path: str,
    code: str,
    error: BaseException | None = None,
) -> None:
    issue = {"path": path, "code": code}
    if error is not None:
        issue["error_type"] = type(error).__name__
    issues.append(issue)


__all__ = ["collect_macro_http_acceptance"]
