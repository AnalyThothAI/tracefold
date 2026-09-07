from __future__ import annotations

import math
from urllib.parse import urlsplit

# The one shape a Feishu custom-bot webhook has. It was written twice -- once in the config model, so
# `tracefold config` can name an unusable webhook before any sender exists, and once in the adapter,
# so the client refuses to post to anything else -- and two copies of one rule can disagree. Neither
# side may own it: `platform` must not import an adapter, and an adapter must not import the config
# model to learn its own provider's URL shape, so the rule lives where both may simply name it.
_FEISHU_WEBHOOK_HOST = "open.feishu.cn"
_FEISHU_WEBHOOK_PATH_PREFIX = "/open-apis/bot/v2/hook/"
# The proxy schemes httpx can route a request through. `socks5h` resolves the hostname at the proxy,
# which is the difference that matters on a host whose DNS cannot answer for the destination.
_PROXY_URL_SCHEMES = frozenset({"http", "https", "socks5", "socks5h"})
SOCKS_PROXY_URL_SCHEMES = frozenset({"socks5", "socks5h"})


def require_nonnegative_float(value: object, *, error_code: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(error_code)
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0:
        raise ValueError(error_code)
    return parsed


def is_feishu_webhook_url(value: str | None) -> bool:
    """Whether this is one Feishu custom-bot hook: the exact origin, one hook id, and nothing else."""

    if value is None:
        return False
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    hook_id = parsed.path.removeprefix(_FEISHU_WEBHOOK_PATH_PREFIX)
    return bool(
        parsed.scheme == "https"
        and parsed.hostname == _FEISHU_WEBHOOK_HOST
        and port in {None, 443}
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
        and parsed.path.startswith(_FEISHU_WEBHOOK_PATH_PREFIX)
        and hook_id
        and "/" not in hook_id
    )


def proxy_url_scheme(value: str | None) -> str | None:
    """The scheme of an outbound proxy URL, or `None` when it is not one this process can route through.

    A proxy URL carries credentials often enough that it is never reported back to an operator, so the
    one thing a caller may learn about it is whether it is usable and which family it belongs to.
    """

    if value is None:
        return None
    try:
        parsed = urlsplit(value.strip())
        _port = parsed.port
    except ValueError:
        return None
    if parsed.scheme not in _PROXY_URL_SCHEMES or not parsed.hostname:
        return None
    if parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        return None
    return parsed.scheme
