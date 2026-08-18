from __future__ import annotations

from tracefold.platform.config.settings import Settings


def gmgn_stream_enabled(settings: Settings) -> bool:
    """Return whether this configuration owns the anonymous GMGN stream."""

    return bool(settings.upstream.channels)


__all__ = ["gmgn_stream_enabled"]
