from __future__ import annotations

from tracefold.app.http.exceptions import ApiBadRequest

WINDOWS = {"5m", "1h", "4h", "24h"}
TOKEN_RADAR_VENUES = {"all", "sol", "eth", "base", "bsc", "cex"}
MAX_RESPONSE_LIST_ITEMS = 100


def _limit(value: int, *, maximum: int = MAX_RESPONSE_LIST_ITEMS, field: str = "limit") -> int:
    parsed = _api_limit_int(value, field=field)
    if parsed < 0:
        raise ApiBadRequest("invalid_limit", field=field)
    return min(parsed, maximum)


def _positive_limit(value: int, *, maximum: int = MAX_RESPONSE_LIST_ITEMS, field: str = "limit") -> int:
    parsed = _api_limit_int(value, field=field)
    if parsed <= 0:
        raise ApiBadRequest("invalid_limit", field=field)
    return min(parsed, maximum)


def _api_limit_int(value: int, *, field: str) -> int:
    if isinstance(value, bool):
        raise ApiBadRequest("invalid_limit", field=field)
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ApiBadRequest("invalid_limit", field=field) from exc


def _handle_set(raw: str) -> set[str]:
    return {item.strip().lstrip("@").lower() for item in raw.split(",") if item.strip()}


def _token_radar_venue(value: str) -> str:
    if value in TOKEN_RADAR_VENUES:
        return value
    raise ApiBadRequest("invalid_venue", field="venue")


def _window(value: str) -> str:
    if value in WINDOWS:
        return value
    raise ApiBadRequest("invalid_window", field="window")


def _post_range(value: str) -> str:
    if value in {"current_window", "since_ignition", "all_history"}:
        return value
    raise ApiBadRequest("invalid_range", field="range")


def _target_type(value: str) -> str | None:
    return value if value in {"Asset", "CexToken"} else None
