"""One expected-failure type for every venue adapter, so a snapshot degrades per venue instead of failing."""

from __future__ import annotations


class VenueExpectedError(RuntimeError):
    """An anticipated venue failure: timeout, transport, HTTP status, or an unusable payload shape.

    ``code`` is a stable identifier for logs and the status surface (``venue_timeout``, ``venue_http_error``,
    ``venue_payload_invalid``, ``venue_blocked``). Never carries response bodies.
    """

    def __init__(self, code: str, *, venue: str, status_code: int | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.venue = venue
        self.status_code = status_code


__all__ = ["VenueExpectedError"]
