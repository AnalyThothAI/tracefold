"""Read a public endpoint's answer under a byte ceiling, and stop reading when it is passed.

A cap applied to `response.content` is not a cap: by the time that attribute exists the whole body is
already in this process's memory, which is the thing the ceiling was supposed to prevent. These two
helpers bound the read itself -- the declared length first, then the bytes as they arrive -- so a
hostile or broken answer costs one chunk over the limit rather than all of it.

Shared by the two #572 adapters because both talk to small public services whose response size nothing
in this repository controls.
"""

from __future__ import annotations

import httpx


class ResponseTooLarge(RuntimeError):
    """The answer declared, or turned out to be, larger than the caller's ceiling."""


def refuse_declared_length(response: httpx.Response, *, max_bytes: int) -> None:
    """Refuse before reading a body whose own `Content-Length` is already over the ceiling."""

    declared = response.headers.get("content-length")
    if declared is None:
        return
    try:
        length = int(declared)
    except ValueError:
        return
    if length > max_bytes:
        raise ResponseTooLarge(f"declared:{length}")


async def read_bounded(response: httpx.Response, *, max_bytes: int) -> bytes:
    """Accumulate a streamed body, stopping the moment it passes the ceiling.

    The response must have been opened with `client.stream(...)`; `aiter_bytes` is what makes this a
    bound rather than a measurement taken too late.
    """

    refuse_declared_length(response, max_bytes=max_bytes)
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > max_bytes:
            raise ResponseTooLarge(f"read:{total}")
        chunks.append(chunk)
    return b"".join(chunks)


__all__ = ["ResponseTooLarge", "read_bounded", "refuse_declared_length"]
